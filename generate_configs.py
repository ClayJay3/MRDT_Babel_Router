import os
import ipaddress

def print_header(title, description=""):
    """Prints a formatted header box to the console for clear UI separation."""
    print(f"\n{'='*60}")
    print(f" {title} ")
    print(f"{'='*60}")
    if description:
        print(f"{description}\n")

def get_input(prompt, example, required=True):
    """
    Prompts the user for input with an example.
    Enforces a non-empty response if 'required' is True.
    """
    while True:
        val = input(f"{prompt}\n  (Example: {example}) -> ").strip()
        if val:
            return val
        if not required:
            return ""
        print("  [!] This field is required. Please provide a value.\n")

def get_int_input(prompt, example):
    """
    Wraps get_input to ensure the user inputs a valid integer.
    Loops until a valid integer is provided.
    """
    while True:
        val = get_input(prompt, example)
        try:
            return int(val)
        except ValueError:
            print("  [!] Please enter a valid number.\n")

def main():
    print_header("MRDT Dynamic Network Config Generator", 
                 "This wizard will ask you questions about your hardware and IP\n"
                 "layout to dynamically generate Cisco, FRRouting, Netplan, and\n"
                 "Linux Traffic Control (QoS) configurations.")

    # ==========================================
    # 1. CORE NETWORK DETAILS
    # ==========================================
    print_header("1. Core Network Details", 
                 "These settings define how your Raspberry Pi talks to your Cisco switch.\n"
                 "This connection uses an OSPF Transit VLAN to pass routes.")
    
    pi_iface = get_input("What is the name of the ethernet adapter on the Raspberry Pis?", "eth0, enp3s0, etc.")
    
    # Rover core settings
    rover_transit_ip = get_input("What IP address should the Rover Pi use for the Transit VLAN?\nInclude the CIDR mask.", "10.99.1.2/30")
    rover_transit_mask = get_input("What is the subnet mask for that Rover Transit IP?", "255.255.255.252")
    rover_switch_trunk = get_input("What is the Rover Cisco Switch port connected to the Pi?", "GigabitEthernet1/10")
    rover_loopback_ip = get_input("What is the Rover Cisco Switch Loopback IP (for Multicast RP)?", "192.168.254.1")
    
    # Base core settings
    base_transit_ip = get_input("What IP address should the Base Pi use for the Transit VLAN?\nInclude the CIDR mask.", "10.99.2.2/30")
    base_transit_mask = get_input("What is the subnet mask for that Base Transit IP?", "255.255.255.252")
    base_switch_trunk = get_input("What is the Base Cisco Switch port connected to the Pi?", "GigabitEthernet1/0/15")
    base_loopback_ip = get_input("What is the Base Cisco Switch Loopback IP (for Multicast RP)?", "192.168.254.2")
    
    # ==========================================
    # 2. WIRELESS LINKS SETUP
    # ==========================================
    print_header("2. Wireless Links Setup", 
                 "We will now configure the Point-to-Point transparent radio bridges.\n"
                 "Each link gets its own VLAN and Base Cost. Babel uses the cost to\n"
                 "prioritize the best links (e.g., 5.8GHz = 100, 900MHz = 500).")
                 
    num_links = get_int_input("How many separate wireless frequency links are you using?", "3")
    links = []
    
    for i in range(num_links):
        print(f"\n--- Configuring Link {i+1} of {num_links} ---")
        
        freq = get_input("Name or Frequency of this link", "5.8GHz, 2.4GHz, 900MHz, etc.")
        vlan = get_input(f"What VLAN ID should be assigned to {freq}?", "58, 24, 900, etc.")
        cost = get_input(f"What is the Babel Base Cost for {freq}? (Lower is preferred)", "100, 250, 500")
        
        # Calculate dynamic example IPs based on the iteration
        rover_ip_example = f"10.0.0.{1 + (i*8)}/29"
        base_ip_example = f"10.0.0.{2 + (i*8)}/29"
        
        rover_ip = get_input(f"What is the Rover Pi's IP on the {freq} link? (Include CIDR)", rover_ip_example)
        base_ip = get_input(f"What is the Basestation Pi's IP on the {freq} link? (Include CIDR)", base_ip_example)
        
        rover_radio_port = get_input(f"What is the Rover Cisco Switch port for the {freq} radio?", "GigabitEthernet1/1")
        base_radio_port = get_input(f"What is the Base Cisco Switch port for the {freq} radio?", "GigabitEthernet1/0/12")
        
        # Store configuration as a dictionary for later file generation
        links.append({
            "freq": freq.replace(" ", "_"), 
            "vlan": vlan, 
            "cost": cost, 
            "rover_ip": rover_ip, 
            "base_ip": base_ip, 
            "rover_radio_port": rover_radio_port,
            "base_radio_port": base_radio_port,
            "channel": i+1
        })

    # ==========================================
    # 3. TRAFFIC ENGINEERING / QOS
    # ==========================================
    print_header("3. Traffic Engineering (VLANs & QoS)", 
                 "We must separate your traffic to ensure high-bandwidth video does\n"
                 "not congest your radio links and kill your low-bandwidth telemetry.\n"
                 "We classify traffic into 'High Priority' (Telemetry/Motors) and\n"
                 "'Low Priority' (Cameras/Science data).")

    high_prio_vlans = []
    low_prio_vlans = []
    
    print("\nLet's add the VLANs located on the Rover:")
    while True:
        v_id = get_input("Enter a Rover VLAN ID (Or leave blank to finish adding VLANs)", "2, 3, 4", required=False)
        if not v_id: 
            break
            
        v_name = get_input(f"What is the name of VLAN {v_id}?", "Telemetry, Cameras, Autonomy")
        v_subnet = get_input(f"What is the IP Subnet for VLAN {v_id}? (Include CIDR)", f"192.168.{v_id}.0/24")
        v_prio = get_input(f"Is {v_name} High (H) or Low (L) Priority?", "H or L").upper()
        
        vlan_data = {"id": v_id, "name": v_name, "subnet": v_subnet}
        
        if v_prio.startswith('H'): 
            high_prio_vlans.append(vlan_data)
        else: 
            low_prio_vlans.append(vlan_data)
            
        print(f"  -> Added {v_name} ({v_subnet}) to routing table.\n")

    print("\nNow for the Basestation side:")
    base_vlan = get_input("What is the Basestation local subnet? (Include CIDR)", "192.168.100.0/24")

    # ==========================================
    # 4. CONFIGURATION GENERATION
    # ==========================================
    out_dir = "generated_configs"
    os.makedirs(out_dir, exist_ok=True)
    print_header("Generating Files...", f"Saving to ./{out_dir}/")

    # --- Pre-calculate values using ipaddress module ---
    # Use ipaddress to avoid collisions and brittle slicing
    rover_iface_obj = ipaddress.IPv4Interface(rover_transit_ip)
    base_iface_obj = ipaddress.IPv4Interface(base_transit_ip)
    
    # Calculate unique Switch IP (assuming Switch is .1 if Pi is .2, or just pick first available)
    rover_switch_ip = str(rover_iface_obj.network.network_address + 1)
    base_switch_ip = str(base_iface_obj.network.network_address + 1)
    
    rover_transit_ip_only = str(rover_iface_obj.ip)
    base_transit_ip_only = str(base_iface_obj.ip)
    
    # Calculate actual network and wildcard masks
    rover_ospf_network = str(rover_iface_obj.network.network_address)
    rover_wildcard = str(rover_iface_obj.network.hostmask)
    base_ospf_network = str(base_iface_obj.network.network_address)
    base_wildcard = str(base_iface_obj.network.hostmask)

    # Compile the allowed VLAN list for trunks (e.g. "58,24,900,99")
    allowed_vlan_str = ",".join([link['vlan'] for link in links]) + ",99"
    
    # Combine high and low priority VLANs for iterating easily later
    all_rover_vlans = high_prio_vlans + low_prio_vlans

    # ---------------------------------------------------------
    # 4.1 Cisco Switches
    # ---------------------------------------------------------
    
    # Generate Rover Switch
    with open(f"{out_dir}/rover_switch.txt", "w") as f:
        f.write("! MRDT Rover Cisco Switch Config\n")
        f.write("no router eigrp 90\n\n")
        
        for link in links: 
            f.write(f"vlan {link['vlan']}\n")
            f.write(f" name {link['freq']}_Link\n")
            
        f.write("vlan 99\n name Pi_Transit_Rover\n\n")
        
        f.write(f"interface {rover_switch_trunk}\n description Trunk to Rover Pi\n")
        f.write(" switchport mode trunk\n")
        f.write(f" switchport trunk allowed vlan {allowed_vlan_str}\n")
        f.write(" no spanning-tree portfast\n")
        f.write(" no spanning-tree bpduguard enable\n\n")
        
        # Generate Layer 2 access ports for the radios
        for link in links:
            f.write(f"interface {link['rover_radio_port']}\n")
            f.write(f" description {link['freq']} Radio Link\n")
            f.write(" switchport\n")
            f.write(" switchport mode access\n")
            f.write(f" switchport access vlan {link['vlan']}\n\n")

        f.write("interface Vlan99\n")
        f.write(f" ip address {rover_switch_ip} {rover_transit_mask}\n")
        f.write(" ip ospf 1 area 0\n\n")
        
        f.write("router ospf 1\n")
        f.write(" router-id 192.168.254.1\n")
        f.write(f" network {rover_ospf_network} {rover_wildcard} area 0\n")
        f.write(f" network {rover_loopback_ip} 0.0.0.0 area 0\n")
        
        # Dynamic wildcard for all subnets
        for vlan in all_rover_vlans: 
            vlan_net = ipaddress.IPv4Network(vlan['subnet'], strict=False)
            f.write(f" network {vlan_net.network_address} {vlan_net.hostmask} area 0\n")

    # Generate Base Switch
    with open(f"{out_dir}/base_switch.txt", "w") as f:
        f.write("! MRDT Base Cisco Switch Config\n")
        f.write("no router eigrp 90\n\n")
        
        for link in links: 
            f.write(f"vlan {link['vlan']}\n")
            f.write(f" name {link['freq']}_Link\n")
            
        f.write("vlan 99\n name Pi_Transit_Base\n\n")
        
        f.write(f"interface {base_switch_trunk}\n description Trunk to Base Pi\n")
        f.write(" switchport trunk encapsulation dot1q\n")
        f.write(" switchport mode trunk\n")
        f.write(f" switchport trunk allowed vlan {allowed_vlan_str}\n")
        f.write(" no spanning-tree portfast\n")
        f.write(" no spanning-tree bpduguard enable\n\n")
        
        # Generate Layer 2 access ports for the radios
        for link in links:
            f.write(f"interface {link['base_radio_port']}\n")
            f.write(f" description {link['freq']} Radio Link\n")
            f.write(" switchport\n")
            f.write(" switchport mode access\n")
            f.write(f" switchport access vlan {link['vlan']}\n\n")

        f.write("interface Vlan99\n")
        f.write(f" ip address {base_switch_ip} {base_transit_mask}\n")
        f.write(" ip ospf 1 area 0\n\n")
        
        f.write("router ospf 1\n")
        f.write(" router-id 192.168.254.2\n")
        f.write(f" network {base_ospf_network} {base_wildcard} area 0\n")
        f.write(f" network {base_loopback_ip} 0.0.0.0 area 0\n")
        
        base_vlan_net = ipaddress.IPv4Network(base_vlan, strict=False)
        f.write(f" network {base_vlan_net.network_address} {base_vlan_net.hostmask} area 0\n")

    # ---------------------------------------------------------
    # 4.2 Linux OS Network Configs (Netplan & Interfaces)
    # ---------------------------------------------------------
    network_sites = [
        ("rover", rover_transit_ip), 
        ("base", base_transit_ip)
    ]
    
    for site, transit_ip in network_sites:
        
        # Modern Ubuntu Network Setup (Netplan)
        with open(f"{out_dir}/{site}_netplan.yaml", "w") as f:
            f.write(f"network:\n  version: 2\n  ethernets:\n    {pi_iface}:\n      dhcp4: true\n  vlans:\n")
            f.write(f"    {pi_iface}.99:\n      id: 99\n      link: {pi_iface}\n      addresses: [{transit_ip}]\n")
            
            for link in links:
                ip = link['rover_ip'] if site == "rover" else link['base_ip']
                f.write(f"    {pi_iface}.{link['vlan']}:\n      id: {link['vlan']}\n      link: {pi_iface}\n      addresses: [{ip}]\n")
        
        # Legacy/Debian Network Setup (/etc/network/interfaces)
        with open(f"{out_dir}/{site}_interfaces.txt", "w") as f:
            f.write(f"auto {pi_iface}.99\n")
            f.write(f"iface {pi_iface}.99 inet static\n")
            f.write(f"  address {transit_ip}\n")
            f.write(f"  vlan-raw-device {pi_iface}\n\n")
            
            for link in links:
                ip_cidr = link['rover_ip'] if site == "rover" else link['base_ip']
                f.write(f"auto {pi_iface}.{link['vlan']}\n")
                f.write(f"iface {pi_iface}.{link['vlan']} inet static\n")
                f.write(f"  address {ip_cidr}\n")
                f.write(f"  vlan-raw-device {pi_iface}\n\n")

    # ---------------------------------------------------------
    # 4.3 FRRouting Configs (OSPF & Babel)
    # ---------------------------------------------------------
    frr_sites = [
        ("rover", rover_transit_ip_only, str(rover_iface_obj.network)), 
        ("base", base_transit_ip_only, str(base_iface_obj.network))
    ]
    
    for site, ip_only, full_network in frr_sites:
        with open(f"{out_dir}/{site}_frr.conf", "w") as f:
            # OSPF Configuration
            f.write(f"! {site.capitalize()} FRR Config\n")
            f.write("router ospf\n")
            f.write(f" ospf router-id {ip_only}\n")
            f.write(f" network {full_network} area 0\n")
            f.write(" redistribute babel\n\n")
            
            # Babel Routing Protocol Configuration
            f.write("router babel\n")
            f.write(" babel diversity\n")
            f.write(" redistribute ospf\n")
            
            for link in links: 
                f.write(f" network {pi_iface}.{link['vlan']}\n")
                
            # Individual Babel Wireless Link Settings
            for link in links:
                f.write(f"\ninterface {pi_iface}.{link['vlan']}\n")
                f.write(" babel type wireless\n")
                f.write(" no babel split-horizon\n")
                f.write(f" babel channel {link['channel']}\n")
                f.write(f" babel rxcost {link['cost']}\n")
                f.write(" babel hello-interval 1000\n")
                f.write(" babel update-interval 4000\n")
                f.write(" babel enable-timestamps\n")
                f.write(" babel max-rtt-penalty 150\n")

    # ---------------------------------------------------------
    # 4.4 Linux Traffic Control (QoS) Scripts
    # ---------------------------------------------------------
    for site in ["rover", "base"]:
        with open(f"{out_dir}/{site}_qos.sh", "w") as f:
            f.write("#!/bin/bash\n")
            f.write("# Apply Traffic Control (QoS) to all wireless subinterfaces\n\n")
            
            # Gather all interfaces (e.g. eth0.58 eth0.24)
            ifaces_string = " ".join([f"{pi_iface}.{link['vlan']}" for link in links])
            
            f.write(f"for iface in {ifaces_string}; do\n")
            f.write("    tc qdisc del dev $iface root 2>/dev/null\n")
            f.write("    tc qdisc add dev $iface root handle 1: prio bands 3\n\n")
            
            # --- High Priority Traffic ---
            f.write("    # BAND 1 (HIGHEST PRIORITY - Telemetry/Motors)\n")
            for vlan in high_prio_vlans:
                direction = "src" if site == "rover" else "dst"
                f.write(f"    tc filter add dev $iface protocol ip parent 1: prio 1 u32 match ip {direction} {vlan['subnet']} flowid 1:1\n")
            
            # --- Low Priority Traffic ---
            f.write("\n    # BAND 3 (LOWEST PRIORITY - Cameras/Science - Dropped if congested)\n")
            for vlan in low_prio_vlans:
                direction = "src" if site == "rover" else "dst"
                f.write(f"    tc filter add dev $iface protocol ip parent 1: prio 3 u32 match ip {direction} {vlan['subnet']} flowid 1:3\n")
                
            f.write("done\n")

    print(f"Success! Generated {num_links*2 + 8} files in the ./generated_configs/ directory.")

if __name__ == "__main__":
    main()