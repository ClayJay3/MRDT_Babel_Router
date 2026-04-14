# MRDT Dynamic Rover Routing Architecture

This repository contains the architecture, configuration generators, and telemetry dashboard for the Mars Rover Design Team's (MRDT) dynamic wireless network.

This system uses a "Router on a Stick" topology, offloading complex wireless routing from Cisco switches to Linux-based WAN edge routers (Raspberry Pis) running FRRouting (Babel) and Linux Traffic Control (QoS).

## 🧠 Why Babel over EIGRP?

Historically, MRDT utilized Cisco's EIGRP protocol across all wireless links. While EIGRP is an enterprise heavyweight for stable, wired environments, it completely breaks down in chaotic, multi-node wireless environments.

### The Problem with EIGRP on Wireless
EIGRP relies on static metrics (Bandwidth and Delay). It assumes that if a link is "up," it is performing flawlessly. If a rover drives behind an obstacle and a 5.8GHz link drops 40% of its packets, EIGRP doesn't notice. It continues blindly sending video data into the void until the link completely drops. Furthermore, marginal wireless links cause EIGRP to constantly drop and rebuild neighbor adjacencies, leading to a phenomenon known as "route flapping," which paralyzes the network.

### The Babel Solution
Babel (RFC 8966) is an advanced distance-vector routing protocol specifically designed for lossy wireless mesh networks.

* **ETX Metric:** Babel uses Expected Transmission Count (ETX) to constantly measure packet loss. If a link degrades, Babel dynamically increases the route cost and seamlessly shifts traffic to a cleaner frequency.
* **Fast Convergence:** Babel uses sequence numbers to ensure loop-free routing and can converge in milliseconds.
* **No Flapping:** Babel degrades gracefully. It shifts traffic before a link completely dies, maintaining absolute persistence for remote rover control.

## 🏗️ Architecture Overview

To support Babel while maintaining our Cisco hardware, we use a decoupled routing strategy:

* **Cisco Switches (LAN Core):** Handle local hardware (Cameras, Motors, Sensors). They run OSPF exclusively to pass local subnets to the Raspberry Pi over a hardwired trunk port.
* **Raspberry Pis (WAN Edge):** Sit on all radio VLANs. They take the OSPF routes from the switch, translate them into Babel, and broadcast them across the 900MHz, 2.4GHz, and 5.8GHz links simultaneously.
* **Quality of Service (QoS):** Because Babel is destination-based, it routes all traffic down the single best link. If forced onto the low-bandwidth 900MHz link, high-bandwidth camera video would crush critical telemetry. We use Linux `tc` (Traffic Control) to strictly prioritize Telemetry VLANs. If the 900MHz link saturates, video frames are instantly dropped by the kernel to guarantee zero-latency joystick control.

## 🚀 Step-by-Step Setup Guide

### Phase 1: Generate Configurations
We have built a dynamic Python script to generate the exact Cisco commands, FRR configurations, and Linux QoS bash scripts based on your specific VLANs and priorities.

1.  Ensure you have Python 3 installed.
2.  Run the configuration generator:
    ```bash
    python3 generate_configs.py
    ```
3.  Answer the prompts in the CLI wizard.
4.  The script will output a `generated_configs/` folder containing everything you need.

### Phase 2: Switch Configuration
1.  Connect to both the Rover and Basestation Cisco switches.
2.  Copy and paste the contents of `generated_configs/rover_switch.txt` and `generated_configs/base_switch.txt` into the respective switch configuration terminals.

### Phase 3: Raspberry Pi Setup
1.  **Install Dependencies:** On both Raspberry Pis, install FRRouting and Flask.
    ```bash
    sudo apt update
    sudo apt install frr frr-pythontools python3-flask
    ```
2.  **Enable Routing:** Edit `/etc/sysctl.conf`, uncomment `net.ipv4.ip_forward=1`, and run `sudo sysctl -p`.
3.  **Enable Daemons:** Edit `/etc/frr/daemons` and change `ospfd=yes` and `babeld=yes`. Restart FRR:
    ```bash
    sudo systemctl restart frr
    ```
4.  **Apply FRR Configs:** Open the FRR shell (`sudo vtysh`) and paste the contents of the generated `_frr.conf` files.
5.  **Apply QoS:** Make the generated QoS scripts executable (`chmod +x rover_qos.sh`) and run them or add them to your Pi's startup routines.

### Phase 4: The Telemetry Dashboard
To monitor link quality, ETX, Latency, and VLAN distribution in real-time, run the included dashboard application on either Pi.

```bash
sudo python3 dashboard.py
```

Access the dashboard via any web browser on the MRDT network: `http://<Pi-IP>:5000`