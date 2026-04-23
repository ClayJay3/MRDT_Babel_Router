# MRDT Dynamic Rover Routing Architecture

This repository contains the architecture, configuration generators, and telemetry dashboard for the Mars Rover Design Team's (MRDT) dynamic wireless network.

This system uses a **"Router on a Stick"** topology, offloading complex wireless routing from Cisco switches to Linux-based WAN edge routers (Raspberry Pis) running **FRRouting (Babel)** and **Linux Traffic Control (QoS)**.

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

## Why This Setup? (The Theory)

### Why Babel over EIGRP?
Historically, MRDT utilized Cisco's EIGRP protocol across all wireless links directly from switch to switch. While EIGRP is an enterprise heavyweight for stable, wired environments, it completely breaks down in multi-node wireless environments.

* **The EIGRP Flaw:** EIGRP relies on static metrics (Bandwidth and Delay). It assumes that if a link is physically "up," it is performing flawlessly. If a rover drives behind an obstacle and a 5.8GHz link drops 40% of its packets, EIGRP doesn't notice. It blindly sends data into the void. Furthermore, marginal links cause EIGRP to constantly drop and rebuild neighbor adjacencies ("route flapping"), paralyzing the network.
* **The Babel Solution:** Babel (RFC 8966) uses Expected Transmission Count (ETX) to constantly measure packet loss. If a link degrades, Babel dynamically increases the route cost and seamlessly shifts traffic to a cleaner frequency in milliseconds.

## Setup Guide

### 1: Generate Configurations

We have built a dynamic Python script to generate the exact Cisco commands, FRR configurations, Linux Network files, and QoS bash scripts based on your specific VLANs and hardware.

1. Ensure you have Python 3 installed.
2. Run the configuration generator:
   ```bash
   python3 generate_configs.py
   ```
3. Follow the instructions.
4. The script will output a `generated_configs/` folder containing everything you need.

### 2: Switch Configuration

1. Connect to both the Rover and Basestation Cisco switches.
2. Copy and paste the contents of `generated_configs/rover_switch.txt` and `generated_configs/base_switch.txt` into the respective switch configuration terminals.

### 3: Raspberry Pi OS Network Setup (VLANs)
We use a foolproof, brute-force script to manage networking. This guarantees the VLAN interfaces spawn immediately on boot, stay up forever, and never disappear if an ethernet cord is temporarily unplugged. 

**Part A: Blindfold NetworkManager**
To ensure NetworkManager doesn't fight your manual configuration or take interfaces down, you must completely block it from seeing your ethernet adapter. 

On **both Pis**, edit the NetworkManager config:
```bash
sudo nano /etc/NetworkManager/conf.d/99-unmanaged-devices.conf
```
Paste this exactly inside the file (assuming your adapter is `eth0`):
```ini
[keyfile]
unmanaged-devices=interface-name:eth0;interface-name:eth0.*
```
Restart NetworkManager:
```bash
sudo systemctl restart NetworkManager
```

**Part B: Install the Brute-Force Network Script**
1. Make the generated network script executable right in the project directory (do this for `rover_force_vlans.sh` on the Rover, and `base_force_vlans.sh` on the Base).
```bash
sudo chmod +x generated_configs/<site>_force_vlans.sh
```
2. Copy the pre-existing systemd service file from the repository's `services/` folder and enable it:
```bash
sudo cp services/<site>_force_vlans.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable <site>_force_vlans.service
sudo systemctl start <site>_force_vlans.service
```
Run `ifconfig` or `ip a` to confirm your interfaces exist permanently.

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

4. **Apply QoS & IP Forwarding:** Make your generated `<site>_qos.sh` executable and use the pre-built service to run it on startup:
   ```bash
   sudo chmod +x generated_configs/<site>_qos.sh
   sudo cp services/<site>_qos.service /etc/systemd/system/
   sudo systemctl enable <site>_qos.service
   sudo systemctl start <site>_qos.service
   ```

### 5: The Telemetry Dashboard

To monitor link quality, ETX, Latency, and VLAN distribution in real-time, run the included dashboard application on either Pi.
```bash
sudo python3 dashboard.py
```
Access the dashboard via any web browser on the MRDT network: `http://<Pi-IP>:5000`