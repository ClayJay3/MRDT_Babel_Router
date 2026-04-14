import os

def print_header(title):
    print(f"\n{'='*50}\n{title}\n{'='*50}")

def get_input(prompt, default=""):
    val = input(f"{prompt} [{default}]: ").strip()
    return val if val else default

def main():
    print_header("MRDT Dynamic Network Config Generator")
    print("This script will generate Cisco, FRRouting, and QoS configurations.")

    # --- COLLECT DATA ---
    print_header("1. Core Network Details")
    transit_subnet = get_input("Transit Subnet (between switch and Pi)", "10.99.0.0")
    
    print_header("2. Wireless Links Setup")
    num_links = int(get_input("How many wireless links?", "3"))
    links = []
    for i in range(num_links):
        print(f"\n--- Link {i+1} ---")
        freq = get_input("Frequency/Name (e.g., 5.8GHz, 900MHz)", ["5.8GHz", "2.4GHz", "900MHz"][i])
        vlan = get_input(f"VLAN ID for {freq}", ["58", "24", "900"][i])
        cost = get_input(f"Babel Base Cost (Lower is better, e.g., 100, 250, 500)", ["100", "250", "500"][i])
        rover_ip = get_input(f"Rover Pi IP for {freq}", f"10.0.0.{1 + (i*8)}/29")
        base_ip = get_input(f"Base Pi IP for {freq}", f"10.0.0.{2 + (i*8)}/29")
        links.append({"freq": freq, "vlan": vlan, "cost": cost, "rover_ip": rover_ip, "base_ip": base_ip, "channel": i+1})

    print_header("3. Traffic Engineering (VLANs & QoS)")
    print("List your Rover VLANs. We will group them by priority for QoS.")
    high_prio_vlans = []
    low_prio_vlans = []
    
    while True:
        v_id = input("\nEnter Rover VLAN ID (or press Enter to finish): ").strip()
        if not v_id: break
        v_name = get_input("VLAN Name (e.g., Telemetry, Cameras)", "VLAN_"+v_id)
        v_sub = get_input(f"Subnet (e.g., 192.168.{v_id}.0/24)", f"192.168.{v_id}.0/24")
        v_prio = get_input("Priority? (H for High/Telemetry, L for Low/Cameras)", "H").upper()
        
        vlan_data = {"id": v_id, "name": v_name, "subnet": v_sub}
        if v_prio == 'H': high_prio_vlans.append(vlan_data)
        else: low_prio_vlans.append(vlan_data)

    base_vlan = get_input("\nBasestation VLAN Subnet", "192.168.100.0/24")

    # --- GENERATE CONFIGS ---
    os.makedirs("generated_configs", exist_ok=True)
    
    print_header("Generating Files...")

    # 1. Rover Cisco Switch
    with open("generated_configs/rover_switch.txt", "w") as f:
        f.write("! MRDT Rover Cisco Switch Config\nno router eigrp 90\n\n")
        for l in links:
            f.write(f"vlan {l['vlan']}\n name {l['freq']}_Link\n")
        f.write("vlan 99\n name Pi_Transit_Rover\n\n")
        f.write("interface GigabitEthernet1/10\n description Connection to Rover Pi Babel Router\n")
        f.write(" switchport mode trunk\n switchport trunk allowed vlan ")
        f.write(",".join([l['vlan'] for l in links]) + ",99\n\n")
        f.write("interface Vlan99\n ip address 10.99.1.1 255.255.255.252\n ip ospf 1 area 0\n\n")
        f.write("router ospf 1\n router-id 192.168.254.1\n network 10.99.1.0 0.0.0.3 area 0\n")
        for v in high_prio_vlans + low_prio_vlans:
            sub_ip = v['subnet'].split('/')[0]
            f.write(f" network {sub_ip} 0.0.0.255 area 0\n")

    # 2. Base Cisco Switch
    with open("generated_configs/base_switch.txt", "w") as f:
        f.write("! MRDT Base Cisco Switch Config\nno router eigrp 90\n\n")
        for l in links:
            f.write(f"vlan {l['vlan']}\n name {l['freq']}_Link\n")
        f.write("vlan 99\n name Pi_Transit_Base\n\n")
        f.write("interface GigabitEthernet1/0/15\n description Connection to Base Pi Babel Router\n")
        f.write(" switchport trunk encapsulation dot1q\n switchport mode trunk\n switchport trunk allowed vlan ")
        f.write(",".join([l['vlan'] for l in links]) + ",99\n\n")
        f.write("interface Vlan99\n ip address 10.99.2.1 255.255.255.252\n ip ospf 1 area 0\n\n")
        f.write("router ospf 1\n router-id 192.168.254.2\n network 10.99.2.0 0.0.0.3 area 0\n")
        f.write(f" network {base_vlan.split('/')[0]} 0.0.0.255 area 0\n")

    # 3. FRR Configs
    for site, ip in [("rover", "10.99.1.2"), ("base", "10.99.2.2")]:
        with open(f"generated_configs/{site}_frr.conf", "w") as f:
            f.write(f"! {site.capitalize()} FRR Config\nrouter ospf\n ospf router-id {ip}\n network {ip[:-1]}0/30 area 0\n redistribute babel\n\n")
            f.write("router babel\n babel diversity\n redistribute ospf\n")
            for l in links: f.write(f" network eth0.{l['vlan']}\n")
            for l in links:
                f.write(f"\ninterface eth0.{l['vlan']}\n babel type wireless\n no babel split-horizon\n")
                f.write(f" babel channel {l['channel']}\n babel rxcost {l['cost']}\n")
                f.write(" babel hello-interval 1000\n babel update-interval 4000\n babel enable-timestamps\n babel max-rtt-penalty 150\n")

    # 4. QoS Scripts
    for site in ["rover", "base"]:
        with open(f"generated_configs/{site}_qos.sh", "w") as f:
            f.write("#!/bin/bash\n# Apply QoS to all wireless subinterfaces\n\n")
            f.write("for iface in " + " ".join([f"eth0.{l['vlan']}" for l in links]) + "; do\n")
            f.write("    tc qdisc del dev $iface root 2>/dev/null\n")
            f.write("    tc qdisc add dev $iface root handle 1: prio bands 3\n\n")
            
            f.write("    # BAND 1 (HIGHEST PRIORITY)\n")
            for v in high_prio_vlans:
                direction = "src" if site == "rover" else "dst"
                f.write(f"    tc filter add dev $iface protocol ip parent 1: prio 1 u32 match ip {direction} {v['subnet']} flowid 1:1\n")
            
            f.write("\n    # BAND 3 (LOWEST PRIORITY - DROPPED FIRST)\n")
            for v in low_prio_vlans:
                direction = "src" if site == "rover" else "dst"
                f.write(f"    tc filter add dev $iface protocol ip parent 1: prio 3 u32 match ip {direction} {v['subnet']} flowid 1:3\n")
            f.write("done\n")

    print("\nDone! Check the 'generated_configs/' directory for your deployment files.")

if __name__ == "__main__":
    main()