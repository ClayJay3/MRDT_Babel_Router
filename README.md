# MRDT Dynamic Rover Routing Architecture

This repository contains the architecture, configuration generators, and telemetry dashboard for the Mars Rover Design Team's (MRDT) dynamic wireless network.

This system uses a **"Router on a Stick"** topology, offloading complex wireless routing from Cisco switches to Linux-based WAN edge routers (Raspberry Pis) running **FRRouting (Babel)** and **Linux Traffic Control (QoS)**.

---

## Architecture & Topology

To understand this network, you must understand the division of labor. We use **Cisco Switches** for high-speed local routing, Power over Ethernet (PoE), and hardware reliability. We use **Raspberry Pis (Linux)** for intelligent, chaotic-environment wireless routing and traffic shaping. 

We bridge these two systems using a **Router on a Stick** design.

### The Topology
```text
[Cameras, Motors, Sensors] --(VLANs 2, 3, 4)--> [ Cisco Switch (Rover) ]
                                                       |
                                 (802.1Q Trunk Port: VLAN 99, 24, 900)
                                                       |
                                                [ Raspberry Pi ]
                                                (Babel WAN Router)
                                                       |
                                 +---------------------+---------------------+
                          (VLAN 900)                                  (VLAN 24)
                        [900MHz Radio]                             [2.4GHz Radio]
                              |                                          |
                              ~~ ( Chaotic Wireless Gap w/ Babel ) ~~
                              |                                          |
                        [900MHz Radio]                             [2.4GHz Radio]
                          (VLAN 900)                                  (VLAN 24)
                                 +---------------------+---------------------+
                                                       |
                                                [ Raspberry Pi ]
                                                (Babel WAN Router)
                                                       |
                                 (802.1Q Trunk Port: VLAN 99, 24, 900)
                                                       |
[Operator Laptops, ROS] <-------(VLAN 100)------ [ Cisco Switch (Base) ]
```

### How the Data Flows (Step-by-Step)
1. **Local Ingestion:** A camera on the rover generates a video frame on VLAN 4 (Cameras). 
2. **Transit Routing (OSPF):** The Cisco switch receives the frame. It looks at its routing table (populated locally by OSPF) and routes the packet onto the **Transit VLAN (VLAN 99)**, sending it up the trunk wire to the Raspberry Pi.
3. **Intelligent Path Selection (Babel):** The Raspberry Pi receives the packet on `eth0.99`. The Linux kernel consults its routing table (managed by the Babel routing daemon). Babel knows that the 2.4GHz link currently has 2% packet loss, but the 900MHz link is too slow. It chooses the 2.4GHz link.
4. **Traffic Shaping (QoS):** Before the Pi sends the packet out, Linux Traffic Control (`tc`) intercepts it. It sees this is video data (Low Priority). If the joystick/telemetry (High Priority) is also trying to send data right now, `tc` holds the video frame in a queue to guarantee the joystick data goes first.
5. **Layer 2 Egress:** The Pi sends the frame out `eth0.24` (the 2.4GHz subinterface). This adds a VLAN 24 tag to the packet and drops it back down the same physical trunk wire to the Cisco switch.
6. **Radio Transmission:** The Cisco switch sees the VLAN 24 tag and immediately switches it out of the physical port connected to the 2.4GHz radio. 
7. **The Reverse:** The Basestation receives the frame via the Base radio, sends it to the Base Pi for routing, which drops it onto the Base Transit VLAN (99) to the Base Cisco switch, which finally delivers it to the Operator Computer (VLAN 100).

---

## Why This Setup? (The Theory)

### Why Babel over EIGRP?
Historically, MRDT utilized Cisco's EIGRP protocol across all wireless links directly from switch to switch. While EIGRP is an enterprise heavyweight for stable, wired environments, it completely breaks down in multi-node wireless environments.
* **The EIGRP Flaw:** EIGRP relies on static metrics (Bandwidth and Delay). It assumes that if a link is physically "up," it is performing flawlessly. If a rover drives behind an obstacle and a 5.8GHz link drops 40% of its packets, EIGRP doesn't notice. It blindly sends data into the void. Furthermore, marginal links cause EIGRP to constantly drop and rebuild neighbor adjacencies ("route flapping"), paralyzing the network.
* **The Babel Solution:** Babel (RFC 8966) uses Expected Transmission Count (ETX) to constantly measure packet loss. If a link degrades, Babel dynamically increases the route cost and seamlessly shifts traffic to a cleaner frequency in milliseconds. 

### Why Hybrid? (Why not just switches, or just Pis?)
* **Cisco doesn't speak Babel.** Enterprise switches are not built for lossy mesh networks. 
* **Pis don't have enough ports or power.** We need the Cisco switch to provide PoE to devices, and to handle high-speed local switching that would overwhelm a Pi's CPU.
* **Linux QoS is superior.** The Linux kernel's Traffic Control (`tc`) allows for deep, programmatic packet queuing. We can ensure zero-latency joystick control even when forced onto backup 900MHz links by artificially throttling camera data. 

### Hardware Agnostic (You Don't Need Cisco!)
While this documentation explicitly references Cisco switches (as that is MRDT's current hardware), **expensive enterprise Cisco hardware is not strictly required.** You can deploy this exact "Router on a Stick" architecture using almost any Layer 3 smart managed switch. As long as your switch supports **VLAN tagging (802.1Q)**, **Inter-VLAN routing**, and **OSPF**, it will integrate perfectly with the Babel edge routers. 

### Multicast Support (ROS)
Some camera setups and Robot Operating System (ROS) heavily relies on Multicast traffic to publish topics. MRDT doesn't use ROS, we still need a way to ensure this works. Babel and OSPF only handle Unicast routing. To solve this, we run **PIM (Protocol Independent Multicast)** natively on the Linux Pis via FRR, seamlessly bridging the Cisco Multicast Rendezvous Points (RPs) across the wireless gap.

---

## Setup Guide

### 1: Generate Configurations
We have built a dynamic Python script to generate the exact Cisco commands, FRR configurations, Linux Network files, and QoS bash scripts based on your specific VLANs and hardware.

1. Ensure you have Python 3 installed.
2. Run the configuration generator:
   ```bash
   python3 generate_configs.py
   ```
3. Follow the instructions. *Note: Ensure you know if your Pi uses `eth0` (Pi 4) or `end0` (Pi 5)!*
4. The script will output a `generated_configs/` folder containing everything you need.

### 2: Switch Configuration
1. Connect to both the Rover and Basestation Cisco switches.
2. Copy and paste the contents of `generated_configs/rover_switch.txt` and `generated_configs/base_switch.txt` into the respective switch configuration terminals.

### 3: Raspberry Pi OS Network Setup (VLANs)
The Raspberry Pis need to know how to tag traffic for the Cisco switches using VLAN subinterfaces. Depending on your OS, apply the configuration differently:

* **If using Ubuntu Server (Netplan):**
  Copy the generated `_netplan.yaml` to `/etc/netplan/01-netcfg.yaml`.
  *(Note: If Cloud-Init is overwriting your network, run `echo "network: {config: disabled}" | sudo tee /etc/cloud/cloud.cfg.d/99-disable.cfg` first).* Then run `sudo netplan apply`.

* **If using Older Debian 11 / Legacy Pi OS:** Append the contents of the generated `_interfaces.txt` to `/etc/network/interfaces` and reboot.

### 4: FRRouting & QoS Setup

1. **Install Dependencies:** On both Raspberry Pis:
   ```bash
   sudo apt update
   sudo apt install frr frr-pythontools python3-flask
   ```

2. **Enable Daemons:** FRR needs Zebra (to inject routes into the Linux kernel), OSPF, Babel, and PIM (for Multicast).
   Manually open `/etc/frr/daemons` in a text editor on both Pis and change the following lines to `yes`:
   ```text
   zebra=yes
   ospfd=yes
   babeld=yes
   pimd=yes
   ```
   Then restart the service:
   ```bash
   sudo systemctl restart frr
   ```

3. **Apply FRR Configs:** Open the FRR shell (`sudo vtysh`), type `configure terminal`, and paste the contents of the generated `_frr.conf` files. Then type `write memory` to save.

4. **Apply QoS & IP Forwarding:** Make the generated QoS scripts executable (`chmod +x rover_qos.sh`) and run them. 
   *(Note: This script automatically enables `net.ipv4.ip_forward=1` in the Linux kernel to allow routing).*
   Configure the script to run on startup (e.g., via a systemd service or cron `@reboot`). **You can just use the already written services in the `services/` folder but you must remember to `sudo chmod +x` the scripts.**

### 5: The Telemetry Dashboard
To monitor link quality, ETX, Latency, and VLAN distribution in real-time, run the included dashboard application on either Pi.

```bash
sudo python3 dashboard.py
```
Access the dashboard via any web browser on the MRDT network: `http://<Pi-IP>:5000`