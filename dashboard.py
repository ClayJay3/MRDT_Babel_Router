"""
MRDT Babel Telemetry Server
---------------------------
This script runs a Flask web server that gathers live network telemetry data 
from Linux interfaces, Traffic Control (tc), iptables, and FRRouting (Babel). 
It serves a dashboard to visualize network link quality, throughput, and routing costs.
"""

from flask import Flask, jsonify, render_template
import subprocess
import json
import time
import re
import os

# Initialize the Flask application
app = Flask(__name__)

# --- TRAFFIC CATEGORY CONFIGURATION ---
# Add, remove, or modify categories here. The backend metrics, API, and 
# frontend dashboard will automatically adjust to track these dynamically!
TRAFFIC_CATEGORIES = {
    "TELEM": {
        "subnets": ["192.168.2.0/24", "192.168.3.0/24"],
        "tc_class": "1:1",     # Linux traffic control priority queue
        "color": "#00BCD4"     # Dashboard graphing color (Cyan)
    },
    "CAM": {
        "subnets": ["192.168.4.0/24"],
        "tc_class": "1:3",
        "color": "#FF9800"     # Orange
    },
    # Example: You could uncomment this to instantly add Autonomy tracking.
    # "AUTONOMY": {
    #     "subnets": ["192.168.5.0/24"],
    #     "tc_class": "1:2",
    #     "color": "#9C27B0"   # Purple
    # }
}

# --- INTERFACE CONFIGURATION ---
# Add, remove, or modify radio links here. The dashboard will automatically
# generate the pipes, graphs, and API endpoints for them.
INTERFACES = {
    "5.8GHz": {
        "device": "eth0.58",
        "color": "#c8102e", # MRDT Red
        "qos_restricted": False
    },
    "2.4GHz": {
        "device": "eth0.24",
        "color": "#e0a800", # Gold
        "qos_restricted": False
    },
    "900MHz": {
        "device": "eth0.900",
        "color": "#005b9f", # Contrast Blue
        "qos_restricted": True # Triggers the red QoS badge in the UI
    }
}

# State dictionaries to track byte counts for calculating live throughput (Mbps)
# We store the last read RX/TX bytes and the exact timestamp of the read.
last_bytes = {details["device"]: {"rx": 0, "tx": 0, "time": time.time()} for details in INTERFACES.values()}
last_tx_vlan_bytes = {details["device"]: {"categories": {cat: 0 for cat in TRAFFIC_CATEGORIES}, "time": time.time()} for details in INTERFACES.values()}
last_rx_vlan_bytes = {details["device"]: {"categories": {cat: 0 for cat in TRAFFIC_CATEGORIES}, "time": time.time()} for details in INTERFACES.values()}


def get_throughput(iface):
    """
    Calculates live Megabits per second (Mbps) based on Linux sysfs interface counters.
    """
    global last_bytes
    try:
        # Read the raw byte counts directly from the Linux kernel sysfs
        with open(f"/sys/class/net/{iface}/statistics/rx_bytes", "r") as f:
            rx = int(f.read().strip())
        with open(f"/sys/class/net/{iface}/statistics/tx_bytes", "r") as f:
            tx = int(f.read().strip())
            
        current_time = time.time()
        time_diff = current_time - last_bytes[iface]["time"]
        
        # Calculate Mbps
        if time_diff > 0:
            rx_mbps = ((rx - last_bytes[iface]["rx"]) * 8) / (time_diff * 1_000_000)
            tx_mbps = ((tx - last_bytes[iface]["tx"]) * 8) / (time_diff * 1_000_000)
        else:
            rx_mbps, tx_mbps = 0.0, 0.0
            
        # Update the state for the next calculation
        last_bytes[iface] = {"rx": rx, "tx": tx, "time": current_time}
        return round(rx_mbps, 2), round(tx_mbps, 2)
        
    except Exception:
        # Return zeros if the interface doesn't exist or is inaccessible
        return 0.0, 0.0


def get_tx_vlan_throughput(iface):
    """
    Parses Linux tc (Traffic Control) output to get dynamic TX category throughput.
    """
    global last_tx_vlan_bytes
    cat_bytes = {cat: 0 for cat in TRAFFIC_CATEGORIES}
    
    try:
        # Ask Linux for the QoS bucket stats using the 'tc' command
        tc_out = subprocess.check_output(["tc", "-s", "class", "show", "dev", iface], stderr=subprocess.DEVNULL, text=True)
        
        # Regex to find byte counts for dynamic tc classes
        for line in tc_out.split('\n'):
            for cat, details in TRAFFIC_CATEGORIES.items():
                if f"class prio {details['tc_class']}" in line:
                    match = re.search(r'Sent (\d+) bytes', line)
                    if match: 
                        cat_bytes[cat] = int(match.group(1))

        current_time = time.time()
        time_diff = current_time - last_tx_vlan_bytes[iface]["time"]
        cat_mbps = {cat: 0.0 for cat in TRAFFIC_CATEGORIES}

        # Calculate Megabits per second
        if time_diff > 0:
            for cat in TRAFFIC_CATEGORIES:
                mbps = ((cat_bytes[cat] - last_tx_vlan_bytes[iface]["categories"][cat]) * 8) / (time_diff * 1_000_000)
                cat_mbps[cat] = max(0.0, round(mbps, 2))

        # Update the state for the next calculation
        last_tx_vlan_bytes[iface] = {"categories": cat_bytes, "time": current_time}
        return cat_mbps
        
    except Exception:
        return {cat: 0.0 for cat in TRAFFIC_CATEGORIES}


def get_rx_vlan_throughput(base_iface):
    """
    Actively detects incoming (RX) VLAN throughput using dynamic iptables subnets.
    Reads the exact byte counters from the custom MRDT_RX_ACCT chain.
    """
    global last_rx_vlan_bytes
    cat_bytes = {cat: 0 for cat in TRAFFIC_CATEGORIES}
    
    try:
        # Read the exact byte counters (-x), numeric format (-n), verbose (-v)
        out = subprocess.check_output(["iptables", "-t", "mangle", "-L", "MRDT_RX_ACCT", "-v", "-n", "-x"], stderr=subprocess.DEVNULL, text=True)
        
        for line in out.split('\n'):
            parts = line.split()
            # Expected parsed format:
            # ['10', '1500', 'all', '--', 'eth0.58', '*', '0.0.0.0/0', '192.168.2.0/24']
            if len(parts) >= 8 and parts[4] == base_iface:
                bytes_count = int(parts[1])
                src_ip = parts[6]
                dst_ip = parts[7]
                
                # Dynamically check subnets to bucket the bytes
                for cat, details in TRAFFIC_CATEGORIES.items():
                    if any(sub in src_ip or sub in dst_ip for sub in details["subnets"]):
                        cat_bytes[cat] += bytes_count
                    
    except Exception:
        pass

    # CALCULATION Phase
    current_time = time.time()
    time_diff = current_time - last_rx_vlan_bytes[base_iface]["time"]
    cat_mbps = {cat: 0.0 for cat in TRAFFIC_CATEGORIES}

    if time_diff > 0:
        for cat in TRAFFIC_CATEGORIES:
            mbps = ((cat_bytes[cat] - last_rx_vlan_bytes[base_iface]["categories"][cat]) * 8) / (time_diff * 1_000_000)
            cat_mbps[cat] = max(0.0, round(mbps, 2))
        
        last_rx_vlan_bytes[base_iface] = {"categories": cat_bytes, "time": current_time}
        return cat_mbps
    
    return cat_mbps


def get_babel_data():
    """
    Pulls live routing and neighbor data from FRRouting (FRR) via vtysh.
    """
    data = {}
    
    # Initialize default states for all tracked interfaces
    for details in INTERFACES.values():
        iface_name = details["device"]
        data[iface_name] = {"etx": "N/A", "rtt": "N/A", "up": False, "active": False}
            
    # 1. Fetch neighbor link quality (ETX/RTT)
    try:
        neigh_out = subprocess.check_output(["vtysh", "-c", "show babel neighbor json"], text=True)
        neighbors = json.loads(neigh_out)
        
        for neigh in neighbors.values(): 
            if isinstance(neigh, list):
                for n in neigh:
                    iface = n.get("interface")
                    if iface in data:
                        data[iface]["etx"] = n.get("rxcost", "N/A")
                        data[iface]["rtt"] = n.get("rtt", "N/A")
                        data[iface]["up"] = n.get("state") == "Up"
    except Exception:
        pass # Silently pass if FRR isn't running or vtysh fails

    # 2. Fetch active routing table to see which link is currently selected
    try:
        route_out = subprocess.check_output(["vtysh", "-c", "show babel route json"], text=True)
        routes = json.loads(route_out)
        
        for prefix, paths in routes.items():
            for path in paths:
                if path.get("installed") is True:
                    iface = path.get("interface")
                    if iface in data:
                        data[iface]["active"] = True
    except Exception:
        pass

    return data


@app.route('/api/stats')
def api_stats():
    """
    API Endpoint: Gathers all telemetry data and returns it as a JSON payload.
    """
    babel_data = get_babel_data()
    payload = {}
    
    for name, details in INTERFACES.items():
        iface = details["device"]
        
        # Fetch current metrics for each interface
        rx, tx = get_throughput(iface)
        tx_cats = get_tx_vlan_throughput(iface)
        rx_cats = get_rx_vlan_throughput(iface)
        
        # Build category summary object
        category_stats = {}
        for cat in TRAFFIC_CATEGORIES:
            category_stats[cat] = {
                "tx_mbps": tx_cats.get(cat, 0.0),
                "rx_mbps": rx_cats.get(cat, 0.0)
            }
        
        # Build the JSON response structure
        payload[name] = {
            "interface": iface,
            "rx_mbps": rx,
            "tx_mbps": tx,
            "categories": category_stats,
            "etx": babel_data.get(iface, {}).get("etx", "N/A"),
            "rtt": babel_data.get(iface, {}).get("rtt", "N/A"),
            "status": "UP" if babel_data.get(iface, {}).get("up", False) else "DOWN",
            "active": babel_data.get(iface, {}).get("active", False)
        }
        
    return jsonify(payload)


@app.route('/')
def index():
    """
    Web Endpoint: Serves the main HTML dashboard.
    Injects the dynamic TRAFFIC_CATEGORIES and INTERFACES dictionaries into the HTML.
    Assumes index.html is located in the 'templates' folder.
    """
    return render_template('index.html', categories=TRAFFIC_CATEGORIES, interfaces=INTERFACES)


def init_iptables():
    """
    Sets up dynamic iptables rules based on TRAFFIC_CATEGORIES to track RX packets.
    This does not drop or alter traffic; it only creates accounting buckets.
    """
    try:
        # Flush and recreate the accounting chain safely
        subprocess.run(["iptables", "-t", "mangle", "-F", "MRDT_RX_ACCT"], stderr=subprocess.DEVNULL)
        subprocess.run(["iptables", "-t", "mangle", "-N", "MRDT_RX_ACCT"], stderr=subprocess.DEVNULL)
        
        # Ensure it's linked from PREROUTING so it sees traffic before any routing decisions
        subprocess.run(["iptables", "-t", "mangle", "-D", "PREROUTING", "-j", "MRDT_RX_ACCT"], stderr=subprocess.DEVNULL)
        subprocess.run(["iptables", "-t", "mangle", "-I", "PREROUTING", "1", "-j", "MRDT_RX_ACCT"], stderr=subprocess.DEVNULL)
        
        # Create counting rules for each interface and subnet dynamically
        for details in INTERFACES.values():
            iface = details["device"]
            for cat, cat_details in TRAFFIC_CATEGORIES.items():
                for subnet in cat_details["subnets"]:
                    # Match as source (Accounts for traffic returning from the network)
                    subprocess.run(["iptables", "-t", "mangle", "-A", "MRDT_RX_ACCT", "-i", iface, "-s", subnet], stderr=subprocess.DEVNULL)
                    # Match as destination (Accounts for traffic entering heading into the subnets)
                    subprocess.run(["iptables", "-t", "mangle", "-A", "MRDT_RX_ACCT", "-i", iface, "-d", subnet], stderr=subprocess.DEVNULL)
                
        print("[+] Successfully initialized dynamic iptables RX accounting rules.")
    except Exception as e:
        print(f"[-] Failed to initialize iptables: {e}")


if __name__ == '__main__':
    # Initialize our packet tracking rules before starting the server
    init_iptables()
    
    # Start the Flask development server on all available network interfaces
    app.run(host='0.0.0.0', port=5000, debug=False)