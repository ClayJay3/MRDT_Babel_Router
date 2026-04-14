# MRDT Dynamic Rover Routing Architecture

This repository contains the architecture, configuration generators, and telemetry dashboard for the Mars Rover Design Team's (MRDT) dynamic wireless network.

This system uses a **"Router on a Stick"** topology, offloading complex wireless routing from Cisco switches to Linux-based WAN edge routers (Raspberry Pis) running **FRRouting (Babel)** and **Linux Traffic Control (QoS)**.

## Why Babel over EIGRP?

Historically, MRDT utilized Cisco's EIGRP protocol across all wireless links. While EIGRP is an enterprise heavyweight for stable, wired environments, it breaks down in chaotic, multi-node wireless environments.

### The Problem with EIGRP on Wireless

EIGRP relies on static metrics (**Bandwidth** and **Delay**). It assumes that if a link is "up," it is performing flawlessly. If a rover drives behind an obstacle and a 5.8GHz link drops 40% of its packets, EIGRP doesn't notice. It continues blindly sending video data into the void until the link completely drops. Furthermore, marginal wireless links cause EIGRP to constantly drop and rebuild neighbor adjacencies, leading to a phenomenon known as "route flapping," which paralyzes the network.

### The Babel Solution

Babel (RFC 8966) is an advanced distance-vector routing protocol specifically designed for lossy wireless mesh networks.

* **ETX Metric:** Babel uses Expected Transmission Count (ETX) to constantly measure packet loss. If a link degrades, Babel dynamically increases the route cost and seamlessly shifts traffic to a cleaner frequency.

* **Fast Convergence:** Babel uses sequence numbers to ensure loop-free routing and can converge in milliseconds.

* **Traffic Engineering:** By coupling Babel with Linux QoS, we can prioritize low-bandwidth telemetry over high-bandwidth camera feeds, ensuring zero-latency joystick control even when forced onto backup 900MHz links.

## Setup Guide

### 1: Generate Configurations

We have built a dynamic Python script to generate the exact Cisco commands, FRR configurations, Linux Netplan files, and QoS bash scripts based on your specific VLANs and priorities.

1. Ensure you have Python 3 installed.

2. Run the configuration generator:

   ```
   python3 generate_configs.py
   
   ```

3. Follow the instructions and provide your network details.

4. The script will output a `generated_configs/` folder containing everything you need.

### 2: Switch Configuration

1. Connect to both the Rover and Basestation Cisco switches.

2. Copy and paste the contents of `generated_configs/rover_switch.txt` and `generated_configs/base_switch.txt` into the respective switch configuration terminals.

### 3: Raspberry Pi OS Network Setup (VLANs)

The Raspberry Pis need to know how to tag traffic for the Cisco switches using VLAN subinterfaces.

* **If using Ubuntu (Netplan):** Copy the generated `rover_netplan.yaml` to `/etc/netplan/01-netcfg.yaml` and run `sudo netplan apply`.

* **If using older Debian/Raspberry Pi OS:** Append the contents of the generated `_interfaces.txt` to `/etc/network/interfaces` and reboot.

### 4: FRRouting & QoS Setup

1. **Install Dependencies:** On both Raspberry Pis:

   ```
   sudo apt update
   sudo apt install frr frr-pythontools python3-flask
   
   ```

2. **Enable Routing:** Edit `/etc/sysctl.conf`, uncomment `net.ipv4.ip_forward=1`, and run `sudo sysctl -p`.

3. **Enable Daemons:** Edit `/etc/frr/daemons` and change `ospfd=yes` and `babeld=yes`. Restart FRR (`sudo systemctl restart frr`).

4. **Apply FRR Configs:** Open the FRR shell (`sudo vtysh`) and paste the contents of the generated `_frr.conf` files.

5. **Apply QoS:** Make the generated QoS scripts executable (`chmod +x rover_qos.sh` and `chmod +x base_qos.sh`) and configure them to run on startup (e.g., via a systemd service or cron `@reboot`).

### 5: The Telemetry Dashboard

To monitor link quality, ETX, Latency, and VLAN distribution in real-time, run the included dashboard application on either Pi.

```
sudo python3 dashboard.py

```

Access the dashboard via any web browser on the MRDT network: `http://<Pi-IP>:5000`