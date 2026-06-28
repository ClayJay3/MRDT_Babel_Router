#!/usr/bin/env python3
"""
MRDT Dynamic Network Config Generator
=====================================

Generates every config file needed for the MRDT "Router on a Stick" wireless
network: Cisco switch configs, Linux VLAN configs (Netplan + legacy), FRRouting
(OSPF + Babel + PIM), Linux QoS (HTB), systemd services, and the dashboard
config.

Run interactively (asks questions, every prompt has a sensible default):

    python3 generate_configs.py

Or regenerate the bundled example set with zero questions:

    python3 generate_configs.py --example

Everything lands in ./generated_configs/.
"""

import argparse
import ipaddress
import json
import os

# ---------------------------------------------------------------------------
# Defaults
#
# These mirror the reference MRDT deployment. They are used as the pre-filled
# answer for every interactive prompt, and verbatim in --example mode. Edit
# this block if your standard hardware layout changes.
# ---------------------------------------------------------------------------
DEFAULTS = {
    "rp_address": "192.168.254.1",          # network-wide multicast Rendezvous Point
    "switch_needs_encap": True,             # Catalyst/ISL-capable switches need dot1q pinned
    "enable_rtt_metric": False,             # RTT-based Babel cost (off: avoids load oscillation)
    # Where the repo lives on the Pis (used in the generated systemd unit paths).
    # Assumes a fresh Raspberry Pi OS install with the repo cloned into ~/Documents.
    # Override at the prompt if your username or path differs.
    "install_dir": "/home/pi/Documents/MRDT_Babel_Router",

    "rover": {
        "iface": "eth0",
        "transit_ip": "10.99.1.2/30",
        "switch_trunk": "GigabitEthernet1/10",
        "loopback": "192.168.254.1",
        "local_vlans": [
            {"id": "2", "name": "Telemetry", "subnet": "192.168.2.0/24", "prio": "H"},
            {"id": "3", "name": "Autonomy",  "subnet": "192.168.3.0/24", "prio": "H"},
            {"id": "4", "name": "Cameras",   "subnet": "192.168.4.0/24", "prio": "L"},
        ],
    },
    "base": {
        "iface": "eth0",
        "transit_ip": "10.99.2.2/30",
        "switch_trunk": "GigabitEthernet1/0/12",
        "loopback": "192.168.254.2",
        "local_vlans": [
            # 'N' = neutral: an aggregation subnet that is the endpoint for BOTH
            # high and low traffic, so it must not be used as a QoS classifier.
            {"id": "100", "name": "Operators", "subnet": "192.168.100.0/24", "prio": "N"},
        ],
    },

    # Catalog of selectable wireless bands. Toggle any of them on/off at run time
    # (interactively, or with --bands). Lower 'cost' = more preferred by Babel.
    # 'bw_mbit' is the *real usable* airMAX throughput of the radio (read it from
    # the airOS dashboard, NOT the marketing rate) and drives QoS shaping.
    # Reference hardware: Ubiquiti Rocket M5 / M2 / M900 in transparent bridge mode.
    # Each band has a fixed VLAN + subnet, so any subset you enable stays collision-free.
    # Defaults match the tested 2-radio field setup (2.4 + 900); enable M5 via --bands.
    "links": [
        {"freq": "2.4GHz", "model": "M2",   "enabled": True,  "vlan": "24",  "cost": "250", "bw_mbit": "30",
         "rover_ip": "10.0.0.9/29", "base_ip": "10.0.0.10/29",
         "rover_port": "GigabitEthernet1/2", "base_port": "GigabitEthernet1/0/14"},
        {"freq": "900MHz", "model": "M900", "enabled": True,  "vlan": "900", "cost": "400", "bw_mbit": "10",
         "rover_ip": "10.0.0.1/29", "base_ip": "10.0.0.2/29",
         "rover_port": "GigabitEthernet1/1", "base_port": "GigabitEthernet1/0/13"},
        {"freq": "5.8GHz", "model": "M5",   "enabled": False, "vlan": "58",  "cost": "96",  "bw_mbit": "100",
         "rover_ip": "10.0.0.17/29", "base_ip": "10.0.0.18/29",
         "rover_port": "GigabitEthernet1/3", "base_port": "GigabitEthernet1/0/15"},
    ],
}

OSPF_TAG = 8888  # marks Babel routes once they enter OSPF, so they never loop back

# Set by main() so the prompt helpers know whether to ask anything.
INTERACTIVE = True
# Set by main() from --bands; None means "ask / use catalog defaults".
BANDS = None


# ---------------------------------------------------------------------------
# Prompt helpers
# ---------------------------------------------------------------------------
def header(title, description=""):
    print(f"\n{'=' * 64}\n {title}\n{'=' * 64}")
    if description:
        print(description + "\n")


def ask(prompt, default, validate=None):
    """Ask a question with a default. In --example mode just returns the default."""
    if not INTERACTIVE:
        return default
    while True:
        raw = input(f"{prompt}\n  [{default}] -> ").strip()
        val = raw if raw else default
        if validate:
            err = validate(val)
            if err:
                print(f"  [!] {err}\n")
                continue
        return val


def ask_yes_no(prompt, default):
    d = "y" if default else "n"
    return ask(prompt + " (y/n)", d).strip().lower().startswith("y")


def _is_cidr_host(v):
    try:
        ipaddress.IPv4Interface(v)
        if "/" not in v:
            return "Include the CIDR mask, e.g. 10.99.1.2/30."
        return None
    except ValueError:
        return "Not a valid IPv4 address/mask."


def _is_cidr_net(v):
    try:
        ipaddress.IPv4Network(v, strict=False)
        return None if "/" in v else "Include the CIDR mask, e.g. 192.168.2.0/24."
    except ValueError:
        return "Not a valid IPv4 subnet."


# ---------------------------------------------------------------------------
# IP math helpers
# ---------------------------------------------------------------------------
def peer_host(transit_cidr):
    """The other usable host on a /30-style transit link (the switch's SVI IP)."""
    iface = ipaddress.IPv4Interface(transit_cidr)
    for host in iface.network.hosts():
        if host != iface.ip:
            return str(host)
    return str(iface.network.network_address + 1)


def gateway_of(subnet_cidr):
    """First usable host of a subnet, used as the switch SVI / default gateway."""
    net = ipaddress.IPv4Network(subnet_cidr, strict=False)
    return str(net.network_address + 1)


def netmask_of(cidr):
    return str(ipaddress.IPv4Network(cidr, strict=False).netmask)


def wildcard_of(cidr):
    return str(ipaddress.IPv4Network(cidr, strict=False).hostmask)


def network_of(cidr):
    return str(ipaddress.IPv4Network(cidr, strict=False).network_address)


# ---------------------------------------------------------------------------
# Interactive gathering
# ---------------------------------------------------------------------------
def gather_config():
    cfg = json.loads(json.dumps(DEFAULTS))  # deep copy of the defaults

    if not INTERACTIVE:
        cfg["links"] = select_links(cfg["links"])  # honour --bands
        return cfg

    header("MRDT Dynamic Network Config Generator",
           "Press Enter to accept the [default] shown for each question.\n"
           "The defaults reproduce the standard MRDT layout.")

    # --- Core per-site settings ---
    for site in ("rover", "base"):
        s = cfg[site]
        header(f"{site.upper()} core settings")
        s["iface"] = ask(f"Ethernet adapter on the {site} Pi", s["iface"])
        s["transit_ip"] = ask(f"{site} Pi IP on the OSPF transit VLAN (CIDR)",
                              s["transit_ip"], validate=_is_cidr_host)
        s["switch_trunk"] = ask(f"{site} switch port trunked to the Pi", s["switch_trunk"])
        s["loopback"] = ask(f"{site} switch loopback IP (router-id / RP candidate)",
                            s["loopback"], validate=lambda v: None if _ip_ok(v) else "Invalid IP.")

    cfg["rp_address"] = ask("Network-wide multicast Rendezvous Point (RP) IP\n"
                            "  (must be one of the switch loopbacks above)",
                            cfg["rp_address"], validate=lambda v: None if _ip_ok(v) else "Invalid IP.")
    cfg["switch_needs_encap"] = ask_yes_no(
        "Do your switches need 'switchport trunk encapsulation dot1q'?\n"
        "  (Catalyst 3560/3750/2960S etc: yes. Nexus/dot1q-only: no)",
        cfg["switch_needs_encap"])

    # --- Wireless links ---
    header("Wireless bands",
           "Pick which radios to enable. Lower cost = preferred. bw_mbit is the\n"
           "real usable airMAX throughput (from airOS) and drives QoS shaping.")
    cfg["links"] = select_links(cfg["links"])

    cfg["enable_rtt_metric"] = ask_yes_no(
        "Let RTT influence ROUTING cost? (RTT is always measured and shown on\n"
        "  the dashboard regardless.) Only useful for links with large but STABLE\n"
        "  latency (e.g. satellite). Off is correct for normal radios - it stops\n"
        "  the link in use from being penalised for its own queueing delay",
        cfg["enable_rtt_metric"])

    # --- Local VLANs ---
    for site in ("rover", "base"):
        header(f"{site.upper()} local VLANs",
               "Devices hanging off this site's switch. H = high priority\n"
               "(telemetry/motors), L = low priority (cameras/science),\n"
               "N = neutral (aggregation subnet, e.g. operators - not QoS-classified).")
        vlans = []
        existing = cfg[site]["local_vlans"]
        idx = 0
        while True:
            d = existing[idx] if idx < len(existing) else None
            default_id = d["id"] if d else ""
            v_id = ask(f"{site} VLAN ID (blank to finish)", default_id)
            if not v_id:
                break
            v_name = ask("  Name", d["name"] if d else "Devices")
            v_subnet = ask("  Subnet (CIDR)", d["subnet"] if d else "192.168.0.0/24",
                           validate=_is_cidr_net)
            v_prio = ask("  Priority H, L, or N", d["prio"] if d else "L").upper()[:1]
            if v_prio not in ("H", "L", "N"):
                v_prio = "L"
            vlans.append({"id": v_id, "name": v_name.replace(" ", "_"),
                          "subnet": v_subnet, "prio": v_prio,
                          "gateway": gateway_of(v_subnet)})
            idx += 1
        cfg[site]["local_vlans"] = vlans

    cfg["install_dir"] = ask("Install directory on the Pis (for systemd services)",
                             os.path.dirname(os.path.abspath(__file__)))
    return cfg


def _band_keys(link):
    """Names that --bands accepts for a catalog band (e.g. '5.8ghz', 'm5')."""
    return {link["freq"].lower(), str(link.get("model", "")).lower()} - {""}


def select_links(catalog):
    """Turn the band catalog into the list of enabled links.

    Selection comes from (in order): the --bands flag, then interactive y/n
    prompts, then the catalog's own 'enabled' defaults (used by --example).
    Interactive runs also let you confirm each band's details and add extras.
    """
    # Apply --bands up front so it works with and without --example.
    if BANDS is not None:
        wanted = {b.strip().lower() for b in BANDS if b.strip()}
        known = {k for l in catalog for k in _band_keys(l)}
        for unknown in sorted(wanted - known):
            print(f"  [!] Unknown band '{unknown}' ignored. Known: "
                  f"{', '.join(sorted(known))}")
        for l in catalog:
            l["enabled"] = bool(_band_keys(l) & wanted)

    enabled = []
    for l in catalog:
        if INTERACTIVE and BANDS is None:
            on = ask_yes_no(f"Enable {l['freq']} (Ubiquiti Rocket {l.get('model', '?')})?",
                            l.get("enabled", True))
        else:
            on = l.get("enabled", True)
        if on:
            enabled.append(_confirm_link_details(l) if INTERACTIVE else l)

    if INTERACTIVE:
        i = 0
        while ask_yes_no("Add another (custom) wireless link?", False):
            enabled.append(_confirm_link_details(_blank_link(len(catalog) + i)))
            i += 1

    if not enabled:
        raise SystemExit("[!] No wireless bands enabled - nothing to generate.")
    return enabled


def _confirm_link_details(d):
    """Prompt for one link's settings, pre-filled from defaults d."""
    print(f"\n--- {d['freq']} settings ---")
    return {
        "freq": ask("  Name / frequency", d["freq"]).replace(" ", "_"),
        "model": d.get("model", ""),
        "vlan": ask("  VLAN ID", d["vlan"]),
        "cost": ask("  Babel base cost (lower = preferred)", d["cost"]),
        "bw_mbit": ask("  Usable throughput Mbit/s (airOS capacity)", d["bw_mbit"]),
        "rover_ip": ask("  Rover Pi IP (CIDR)", d["rover_ip"], _is_cidr_host),
        "base_ip": ask("  Base Pi IP (CIDR)", d["base_ip"], _is_cidr_host),
        "rover_port": ask("  Rover switch port", d["rover_port"]),
        "base_port": ask("  Base switch port", d["base_port"]),
    }


def _blank_link(i):
    return {"freq": f"Link{i+1}", "vlan": str(10 + i), "cost": "256", "bw_mbit": "10",
            "rover_ip": f"10.0.0.{1 + i*4}/30", "base_ip": f"10.0.0.{2 + i*4}/30",
            "rover_port": "GigabitEthernet1/1", "base_port": "GigabitEthernet1/1"}


def _ip_ok(v):
    try:
        ipaddress.IPv4Address(v)
        return True
    except ValueError:
        return False


def finalize(cfg):
    """Fill in derived values used across multiple files."""
    for i, l in enumerate(cfg["links"]):
        l.setdefault("channel", i + 1)
    for site in ("rover", "base"):
        for v in cfg[site]["local_vlans"]:
            v.setdefault("gateway", gateway_of(v["subnet"]))
    return cfg


# ---------------------------------------------------------------------------
# Cisco switch configs
# ---------------------------------------------------------------------------
def write_switch(path, cfg, site):
    s = cfg[site]
    links = cfg["links"]
    rp = cfg["rp_address"]
    transit = s["transit_ip"]
    switch_ip = peer_host(transit)
    allowed = ",".join([l["vlan"] for l in links] + ["99"])

    L = []
    L.append(f"! MRDT {site.capitalize()} Cisco Switch Config")
    L.append("! Generated by generate_configs.py")
    L.append("no router eigrp 90")
    L.append("!")
    L.append("ip routing")
    L.append("ip multicast-routing")
    L.append(f"ip pim rp-address {rp}")
    L.append("!")

    # VLAN definitions
    for l in links:
        L.append(f"vlan {l['vlan']}\n name {l['freq']}_Link")
    for v in s["local_vlans"]:
        L.append(f"vlan {v['id']}\n name {v['name']}")
    L.append(f"vlan 99\n name Pi_Transit_{site.capitalize()}")
    L.append("!")

    # Loopback: OSPF router-id source and multicast RP anchor
    L.append("interface Loopback0")
    L.append(f" ip address {s['loopback']} 255.255.255.255")
    L.append(" ip pim sparse-mode")
    L.append(" ip ospf 1 area 0")
    L.append("!")

    # Trunk to the Pi. The Pi does not run STP, so portfast + bpduguard is
    # safe AND skips the ~30s listening/learning delay on every link bounce.
    L.append(f"interface {s['switch_trunk']}")
    L.append(" description Trunk to Pi (Babel router)")
    if cfg["switch_needs_encap"]:
        L.append(" switchport trunk encapsulation dot1q")
    L.append(" switchport mode trunk")
    L.append(f" switchport trunk allowed vlan {allowed}")
    L.append(" spanning-tree portfast trunk")
    L.append(" spanning-tree bpduguard enable")
    L.append("!")

    # Radio access ports. These bridge two switches across the RF gap, so the
    # far switch *does* send BPDUs here -> use bpdufilter (suppress STP), NOT
    # bpduguard. Each VLAN has a single path, so there is no L2 loop to guard.
    for l in links:
        port = l["rover_port"] if site == "rover" else l["base_port"]
        L.append(f"interface {port}")
        L.append(f" description {l['freq']} Radio Link")
        L.append(" switchport mode access")
        L.append(f" switchport access vlan {l['vlan']}")
        L.append(" spanning-tree portfast")
        L.append(" spanning-tree bpdufilter enable")
        L.append("!")

    # SVIs: gateways for each local VLAN + the transit. PIM + OSPF on each.
    for v in s["local_vlans"]:
        L.append(f"interface Vlan{v['id']}")
        L.append(f" description {v['name']} gateway")
        L.append(f" ip address {v['gateway']} {netmask_of(v['subnet'])}")
        L.append(" ip pim sparse-mode")
        L.append(" ip ospf 1 area 0")
        L.append("!")
    L.append("interface Vlan99")
    L.append(f" ip address {switch_ip} {netmask_of(transit)}")
    L.append(" ip pim sparse-mode")
    L.append(" ip ospf 1 area 0")
    L.append("!")

    # OSPF. passive-interface default keeps hellos off the access VLANs (no
    # OSPF neighbours live there); only the transit talks to the Pi.
    L.append("router ospf 1")
    L.append(f" router-id {s['loopback']}")
    L.append(" passive-interface default")
    L.append(" no passive-interface Vlan99")
    L.append(f" network {network_of(transit)} {wildcard_of(transit)} area 0")
    L.append(f" network {s['loopback']} 0.0.0.0 area 0")
    for v in s["local_vlans"]:
        L.append(f" network {network_of(v['subnet'])} {wildcard_of(v['subnet'])} area 0")
    L.append("")

    _write(path, "\n".join(L))


# ---------------------------------------------------------------------------
# FRRouting (OSPF + Babel + PIM)
# ---------------------------------------------------------------------------
def write_frr(path, cfg, site):
    s = cfg[site]
    iface = s["iface"]
    transit = s["transit_ip"]
    L = []
    L.append(f"! {site.capitalize()} FRR Config (OSPF + Babel + PIM)")
    L.append("!")

    # --- Multicast: PIM sparse-mode with a static RP ---
    L.append("router pim")
    L.append(f" rp {cfg['rp_address']} 224.0.0.0/4")
    L.append("!")
    L.append(f"interface {iface}.99")
    L.append(" ip pim")
    L.append("!")

    # --- Loop-breaking route-maps for OSPF <-> Babel mutual redistribution ---
    # Babel routes get tagged when they enter OSPF; that tag is denied on the
    # way back into Babel, so a prefix can never ping-pong between protocols.
    L.append(f"route-map BABEL_TO_OSPF permit 10")
    L.append(f" set tag {OSPF_TAG}")
    L.append("!")
    L.append("route-map OSPF_TO_BABEL deny 10")
    L.append(f" match tag {OSPF_TAG}")
    L.append("route-map OSPF_TO_BABEL permit 20")
    L.append("!")

    # --- OSPF (transit toward the local Cisco switch) ---
    L.append("router ospf")
    L.append(f" ospf router-id {ipaddress.IPv4Interface(transit).ip}")
    L.append(f" network {ipaddress.IPv4Interface(transit).network} area 0")
    L.append(" redistribute babel route-map BABEL_TO_OSPF")
    L.append("!")

    # --- Babel (across the wireless) ---
    L.append("router babel")
    L.append(" babel diversity")
    L.append(" redistribute ipv4 ospf route-map OSPF_TO_BABEL")
    for l in cfg["links"]:
        L.append(f" network {iface}.{l['vlan']}")
    L.append("!")

    # --- Per-link Babel interface tuning ---
    for l in cfg["links"]:
        L.append(f"interface {iface}.{l['vlan']}")
        L.append(" ip pim")
        L.append(" babel wireless")          # ETX-based costing for lossy RF
        L.append(" no babel split-horizon")  # correct for shared wireless
        L.append(f" babel channel {l['channel']}")
        L.append(f" babel rxcost {l['cost']}")
        L.append(" babel hello-interval 1000")  # 1s hellos -> fast failure detection
        # Always measure RTT so the dashboard can show latency. Only let it
        # influence the routing cost when explicitly opted in (off by default,
        # because queueing delay would otherwise destabilise path selection).
        L.append(" babel enable-timestamps")
        if cfg["enable_rtt_metric"]:
            L.append(" babel max-rtt-penalty 150")
        L.append("!")

    _write(path, "\n".join(L))


# ---------------------------------------------------------------------------
# Linux QoS (HTB shaper) + IP forwarding
# ---------------------------------------------------------------------------
def write_qos(path, cfg, site):
    high = [v["subnet"] for s in ("rover", "base") for v in cfg[s]["local_vlans"] if v["prio"] == "H"]
    low = [v["subnet"] for s in ("rover", "base") for v in cfg[s]["local_vlans"] if v["prio"] == "L"]
    iface = cfg[site]["iface"]

    L = ["#!/bin/bash",
         "# MRDT QoS - HTB shaper on each wireless uplink.",
         "# Shaping just under the radio's real rate moves the queue onto the Pi",
         "# (where we control it) instead of the radio, so prioritisation works",
         "# and bufferbloat stays low. Bands: control > high-priority > bulk.",
         "set -u",
         "sysctl -w net.ipv4.ip_forward=1",
         "",
         "# Kernel modules (Raspberry Pi OS / Pi 5 do not autoload these)",
         "modprobe sch_htb 2>/dev/null",
         "modprobe sch_fq_codel 2>/dev/null",
         "modprobe cls_fw 2>/dev/null",
         "modprobe cls_u32 2>/dev/null",
         "",
         "# Mark Babel control traffic (UDP 6696) so it always rides the top class.",
         "# Babel's transport is IPv6 link-local, so the ip6tables rule is the key one.",
         "iptables  -t mangle -D POSTROUTING -p udp --dport 6696 -j MARK --set-mark 10 2>/dev/null",
         "iptables  -t mangle -A POSTROUTING -p udp --dport 6696 -j MARK --set-mark 10",
         "ip6tables -t mangle -D POSTROUTING -p udp --dport 6696 -j MARK --set-mark 10 2>/dev/null",
         "ip6tables -t mangle -A POSTROUTING -p udp --dport 6696 -j MARK --set-mark 10",
         ""]

    # Per-interface usable rate (kbit), ~90% of the configured radio throughput.
    L.append("# Usable rate per link in kbit (~90% of real radio throughput)")
    L.append("declare -A RATE")
    for l in cfg["links"]:
        rate_kbit = int(round(float(l["bw_mbit"]) * 1000 * 0.9))
        L.append(f'RATE["{iface}.{l["vlan"]}"]={rate_kbit}')
    L.append("")

    ifaces = " ".join(f"{iface}.{l['vlan']}" for l in cfg["links"])
    L.append(f"for i in {ifaces}; do")
    L.append('    R=${RATE[$i]}')
    L.append("    tc qdisc del dev $i root 2>/dev/null")
    L.append("    tc qdisc add dev $i root handle 1: htb default 30")
    L.append("    tc class add dev $i parent 1:  classid 1:1  htb rate ${R}kbit ceil ${R}kbit")
    L.append("")
    L.append("    # 1:10 control plane (Babel)   1:20 high priority   1:30 bulk/default")
    L.append("    tc class add dev $i parent 1:1 classid 1:10 htb rate $((R*10/100))kbit ceil ${R}kbit prio 0")
    L.append("    tc class add dev $i parent 1:1 classid 1:20 htb rate $((R*50/100))kbit ceil ${R}kbit prio 1")
    L.append("    tc class add dev $i parent 1:1 classid 1:30 htb rate $((R*40/100))kbit ceil ${R}kbit prio 2")
    L.append("")
    L.append("    # fq_codel leaves keep latency low inside each class")
    L.append("    tc qdisc add dev $i parent 1:10 handle 10: fq_codel")
    L.append("    tc qdisc add dev $i parent 1:20 handle 20: fq_codel")
    L.append("    tc qdisc add dev $i parent 1:30 handle 30: fq_codel")
    L.append("")
    L.append("    # Babel control (fwmark 10, set above) -> top class, IPv4 and IPv6")
    L.append("    tc filter add dev $i parent 1:0 protocol ip   prio 1 handle 10 fw flowid 1:10")
    L.append("    tc filter add dev $i parent 1:0 protocol ipv6 prio 2 handle 10 fw flowid 1:10")
    if high:
        L.append("")
        L.append("    # High priority (telemetry / motors)")
        for sub in high:
            L.append(f"    tc filter add dev $i parent 1:0 protocol ip prio 3 u32 match ip src {sub} flowid 1:20")
            L.append(f"    tc filter add dev $i parent 1:0 protocol ip prio 3 u32 match ip dst {sub} flowid 1:20")
    if low:
        L.append("")
        L.append("    # Bulk (cameras / science) - also the default class")
        for sub in low:
            L.append(f"    tc filter add dev $i parent 1:0 protocol ip prio 4 u32 match ip src {sub} flowid 1:30")
            L.append(f"    tc filter add dev $i parent 1:0 protocol ip prio 4 u32 match ip dst {sub} flowid 1:30")
    L.append("done")
    _write(path, "\n".join(L) + "\n", executable=True)


# ---------------------------------------------------------------------------
# Brute-force VLAN bring-up (Raspberry Pi OS)
# ---------------------------------------------------------------------------
def write_force_vlans(path, cfg, site):
    """Imperatively (re)create the tagged subinterfaces. Guarantees they appear
    instantly on boot and survive a cable unplug. Pair with the NetworkManager
    'unmanaged' rule (see README) so NM doesn't tear them down."""
    s = cfg[site]
    iface = s["iface"]
    L = ["#!/bin/bash",
         "# MRDT brute-force VLAN bring-up. Run at boot via the matching",
         "# *_force_vlans.service. See README for the NetworkManager unmanaged rule.",
         "modprobe 8021q",
         f"ip link set {iface} up",
         "",
         "# Transit VLAN 99 (OSPF to the local Cisco switch)",
         f"ip link add link {iface} name {iface}.99 type vlan id 99 2>/dev/null",
         f"ip addr add {s['transit_ip']} dev {iface}.99 2>/dev/null",
         f"ip link set {iface}.99 up",
         ""]
    for l in cfg["links"]:
        ip = l["rover_ip"] if site == "rover" else l["base_ip"]
        L += [f"# {l['freq']} link (VLAN {l['vlan']})",
              f"ip link add link {iface} name {iface}.{l['vlan']} type vlan id {l['vlan']} 2>/dev/null",
              f"ip addr add {ip} dev {iface}.{l['vlan']} 2>/dev/null",
              f"ip link set {iface}.{l['vlan']} up",
              ""]
    _write(path, "\n".join(L), executable=True)


# ---------------------------------------------------------------------------
# systemd services (written to ./services/ to match the deployment workflow)
# ---------------------------------------------------------------------------
def write_services(services_dir, cfg):
    install = cfg["install_dir"].rstrip("/")
    for site in ("rover", "base"):
        force = f"""[Unit]
Description=MRDT {site.capitalize()} force-create router VLANs
After=network.target

[Service]
Type=oneshot
RemainAfterExit=yes
Environment=PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
ExecStart={install}/generated_configs/{site}_force_vlans.sh

[Install]
WantedBy=multi-user.target
"""
        _write(os.path.join(services_dir, f"{site}_force_vlans.service"), force)

        # QoS runs after the VLANs exist (force_vlans) and FRR is up.
        qos = f"""[Unit]
Description=MRDT {site.capitalize()} QoS (HTB) setup
After={site}_force_vlans.service frr.service
Wants={site}_force_vlans.service

[Service]
Type=oneshot
RemainAfterExit=yes
Environment=PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
ExecStart={install}/generated_configs/{site}_qos.sh

[Install]
WantedBy=multi-user.target
"""
        _write(os.path.join(services_dir, f"{site}_qos.service"), qos)

    dash = f"""[Unit]
Description=MRDT Babel Router Dashboard
After=network-online.target frr.service
Wants=network-online.target

[Service]
User=root
WorkingDirectory={install}
Environment=MRDT_DASH_HOST=0.0.0.0
ExecStart=/usr/bin/python3 {install}/dashboard.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
"""
    _write(os.path.join(services_dir, "mrdt_dashboard.service"), dash)


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------
def _write(path, content, executable=False):
    with open(path, "w") as f:
        f.write(content)
    if executable:
        os.chmod(path, 0o755)
    print(f"  wrote {path}")


def main():
    global INTERACTIVE, BANDS
    known = ", ".join(sorted(k for l in DEFAULTS["links"] for k in _band_keys(l)))
    parser = argparse.ArgumentParser(
        description="Generate MRDT network configs.",
        epilog=f"Known bands for --bands: {known}")
    parser.add_argument("--example", action="store_true",
                        help="Generate non-interactively (uses defaults / --bands).")
    parser.add_argument("--bands",
                        help="Comma-separated bands to enable, e.g. '5.8GHz,900MHz' "
                             "or 'M5,M900'. Skips the per-band enable prompts.")
    parser.add_argument("--out", default="generated_configs", help="Output directory.")
    args = parser.parse_args()
    INTERACTIVE = not args.example
    BANDS = args.bands.split(",") if args.bands else None

    cfg = finalize(gather_config())

    out = args.out
    os.makedirs(out, exist_ok=True)
    services_dir = "services"
    os.makedirs(services_dir, exist_ok=True)
    header("Generating files", f"-> ./{out}/ and ./{services_dir}/")

    for site in ("rover", "base"):
        write_switch(os.path.join(out, f"{site}_switch.txt"), cfg, site)
        write_force_vlans(os.path.join(out, f"{site}_force_vlans.sh"), cfg, site)
        write_frr(os.path.join(out, f"{site}_frr.conf"), cfg, site)
        write_qos(os.path.join(out, f"{site}_qos.sh"), cfg, site)
    write_services(services_dir, cfg)

    header("Done", "Next steps:")
    print(" 1. Switches : paste rover_switch.txt / base_switch.txt into each switch.")
    print("               (Assign your device access ports to the local VLANs.)")
    print(" 2. Pi VLANs : blindfold NetworkManager (README), then enable")
    print("               services/<site>_force_vlans.service.")
    print(" 3. FRR      : enable zebra, ospfd, babeld, pimd in /etc/frr/daemons,")
    print("               then load *_frr.conf via vtysh and 'write memory'.")
    print(" 4. QoS+dash : enable services/<site>_qos.service and mrdt_dashboard.service.")
    print(f" 5. RP       : multicast Rendezvous Point is {cfg['rp_address']}.\n")


if __name__ == "__main__":
    main()
