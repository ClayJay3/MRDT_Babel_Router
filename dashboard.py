"""
MRDT Babel Telemetry Server
---------------------------
This script runs a Flask web server that gathers live network telemetry data 
from Linux interfaces, Traffic Control (tc), and FRRouting (Babel). 
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

# Define the network interfaces used for the rover's communication bands
INTERFACES = {
    "5.8GHz": "eth0.58",
    "2.4GHz": "eth0.24",
    "900MHz": "eth0.900"
}

# State dictionaries to track byte counts for calculating live throughput (Mbps)
# We store the last read RX/TX bytes and the exact timestamp of the read.
last_bytes = {iface: {"rx": 0, "tx": 0, "time": time.time()} for iface in INTERFACES.values()}
last_tc_bytes = {iface: {"telem": 0, "cam": 0, "time": time.time()} for iface in INTERFACES.values()}


def get_throughput(iface):
    """
    Calculates live Megabits per second (Mbps) based on Linux sysfs interface counters.
    
    Args:
        iface (str): The name of the network interface (e.g., 'eth0.58').
        
    Returns:
        tuple: (rx_mbps, tx_mbps) rounded to 2 decimal places.
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
        
        # Calculate Mbps: (Bytes * 8 bits/byte) / (Time delta in seconds * 1,000,000 bits/Megabit)
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


def get_qos_throughput(iface):
    """
    Parses Linux tc (Traffic Control) output to get VLAN-specific throughput.
    
    Args:
        iface (str): The name of the network interface.
        
    Returns:
        tuple: (telem_mbps, cam_mbps) rounded to 2 decimal places.
    """
    global last_tc_bytes
    telem_bytes = 0
    cam_bytes = 0
    
    try:
        # Ask Linux for the QoS bucket stats using the 'tc' command
        tc_out = subprocess.check_output(["tc", "-s", "class", "show", "dev", iface], text=True)
        
        # Regex to find byte counts for class 1:1 (Telem/Autonomy) and class 1:3 (Cameras)
        for line in tc_out.split('\n'):
            if "class prio 1:1" in line:
                match = re.search(r'Sent (\d+) bytes', line)
                if match: 
                    telem_bytes = int(match.group(1))
            elif "class prio 1:3" in line:
                match = re.search(r'Sent (\d+) bytes', line)
                if match: 
                    cam_bytes = int(match.group(1))

        current_time = time.time()
        time_diff = current_time - last_tc_bytes[iface]["time"]

        # Calculate Megabits per second
        if time_diff > 0:
            telem_mbps = ((telem_bytes - last_tc_bytes[iface]["telem"]) * 8) / (time_diff * 1_000_000)
            cam_mbps = ((cam_bytes - last_tc_bytes[iface]["cam"]) * 8) / (time_diff * 1_000_000)
        else:
            telem_mbps, cam_mbps = 0.0, 0.0

        # Prevent negative spikes on script restart or tc configuration reload
        telem_mbps = max(0.0, telem_mbps)
        cam_mbps = max(0.0, cam_mbps)

        # Update the state for the next calculation
        last_tc_bytes[iface] = {"telem": telem_bytes, "cam": cam_bytes, "time": current_time}
        return round(telem_mbps, 2), round(cam_mbps, 2)
        
    except Exception:
        # Return zeros if tc isn't running, the interface lacks QoS, or parsing fails
        return 0.0, 0.0


def get_babel_data():
    """
    Pulls live routing and neighbor data from FRRouting (FRR) via vtysh.
    
    Returns:
        dict: A dictionary mapping interface names to their routing metrics 
              (etx, rtt, up status, and whether the route is currently active).
    """
    data = {}
    
    # Initialize default states for all tracked interfaces
    for iface_name in INTERFACES.values():
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
                # If a route is installed, that means Babel is actively sending traffic over it
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
    
    for name, iface in INTERFACES.items():
        # Fetch current metrics for each interface
        rx, tx = get_throughput(iface)
        telem_mbps, cam_mbps = get_qos_throughput(iface)
        
        # Build the JSON response structure
        payload[name] = {
            "interface": iface,
            "rx_mbps": rx,
            "tx_mbps": tx,
            "vlan_telem_mbps": telem_mbps,
            "vlan_cam_mbps": cam_mbps,
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
    Assumes index.html is located in the 'templates' folder.
    """
    return render_template('index.html')


if __name__ == '__main__':
    # Start the Flask development server on all available network interfaces
    app.run(host='0.0.0.0', port=5000, debug=False)