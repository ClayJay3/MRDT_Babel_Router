from flask import Flask, jsonify, render_template_string
import subprocess
import json
import time
import re
import os

app = Flask(__name__)

# The interfaces we configured
INTERFACES = {
    "5.8GHz": "eth0.58",
    "2.4GHz": "eth0.24",
    "900MHz": "eth0.900"
}

# Track byte counts for throughput calculations
last_bytes = {iface: {"rx": 0, "tx": 0, "time": time.time()} for iface in INTERFACES.values()}
last_tc_bytes = {iface: {"telem": 0, "cam": 0, "time": time.time()} for iface in INTERFACES.values()}

def get_throughput(iface):
    """Calculates live Mbps based on Linux sysfs counters."""
    global last_bytes
    try:
        with open(f"/sys/class/net/{iface}/statistics/rx_bytes", "r") as f:
            rx = int(f.read().strip())
        with open(f"/sys/class/net/{iface}/statistics/tx_bytes", "r") as f:
            tx = int(f.read().strip())
            
        current_time = time.time()
        time_diff = current_time - last_bytes[iface]["time"]
        
        if time_diff > 0:
            rx_mbps = ((rx - last_bytes[iface]["rx"]) * 8) / (time_diff * 1_000_000)
            tx_mbps = ((tx - last_bytes[iface]["tx"]) * 8) / (time_diff * 1_000_000)
        else:
            rx_mbps, tx_mbps = 0.0, 0.0
            
        last_bytes[iface] = {"rx": rx, "tx": tx, "time": current_time}
        return round(rx_mbps, 2), round(tx_mbps, 2)
    except Exception:
        return 0.0, 0.0

def get_qos_throughput(iface):
    """Parses Linux tc (Traffic Control) to get VLAN-specific throughput."""
    global last_tc_bytes
    telem_bytes = 0
    cam_bytes = 0
    try:
        # Ask Linux for the QoS bucket stats
        tc_out = subprocess.check_output(["tc", "-s", "class", "show", "dev", iface], text=True)
        
        # Regex to find byte counts for class 1:1 (Telem/Autonomy) and class 1:3 (Cameras)
        for line in tc_out.split('\n'):
            if "class prio 1:1" in line:
                match = re.search(r'Sent (\d+) bytes', line)
                if match: telem_bytes = int(match.group(1))
            elif "class prio 1:3" in line:
                match = re.search(r'Sent (\d+) bytes', line)
                if match: cam_bytes = int(match.group(1))

        current_time = time.time()
        time_diff = current_time - last_tc_bytes[iface]["time"]

        if time_diff > 0:
            telem_mbps = ((telem_bytes - last_tc_bytes[iface]["telem"]) * 8) / (time_diff * 1_000_000)
            cam_mbps = ((cam_bytes - last_tc_bytes[iface]["cam"]) * 8) / (time_diff * 1_000_000)
        else:
            telem_mbps, cam_mbps = 0.0, 0.0

        # Prevent negative spikes on script restart/tc reload
        telem_mbps = max(0.0, telem_mbps)
        cam_mbps = max(0.0, cam_mbps)

        last_tc_bytes[iface] = {"telem": telem_bytes, "cam": cam_bytes, "time": current_time}
        return round(telem_mbps, 2), round(cam_mbps, 2)
    except Exception:
        # Return 0 if tc isn't running or parsing fails
        return 0.0, 0.0

def get_babel_data():
    """Pulls live routing data from FRRouting."""
    data = {}
    try:
        neigh_out = subprocess.check_output(["vtysh", "-c", "show babel neighbor json"], text=True)
        neighbors = json.loads(neigh_out)
        for iface_name in INTERFACES.values():
            data[iface_name] = {"etx": "N/A", "rtt": "N/A", "up": False, "active": False}
            
        for neigh in neighbors.values(): 
            if isinstance(neigh, list):
                for n in neigh:
                    iface = n.get("interface")
                    if iface in data:
                        data[iface]["etx"] = n.get("rxcost", "N/A")
                        data[iface]["rtt"] = n.get("rtt", "N/A")
                        data[iface]["up"] = n.get("state") == "Up"
    except Exception:
        pass 

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
    babel_data = get_babel_data()
    payload = {}
    for name, iface in INTERFACES.items():
        rx, tx = get_throughput(iface)
        telem_mbps, cam_mbps = get_qos_throughput(iface)
        
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
    return render_template_string(HTML_TEMPLATE)

# --- FRONTEND HTML/JS/CSS ---
HTML_TEMPLATE = """
<!DOCTYPE html>
<html>
<head>
    <title>MRDT Babel Telemetry</title>
    <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
    <style>
        body { font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; background: #121212; color: #fff; margin: 0; padding: 20px; overflow-x: hidden; }
        h1 { text-align: center; color: #4CAF50; letter-spacing: 2px; margin-bottom: 20px; text-transform: uppercase; }
        
        /* Summary HUD */
        .hud-container { display: flex; justify-content: center; gap: 30px; margin-bottom: 40px; }
        .hud-card { background: #1e1e1e; border: 1px solid #444; border-radius: 8px; padding: 15px 30px; text-align: center; width: 200px; box-shadow: 0 4px 10px rgba(0,0,0,0.5); transition: all 0.3s ease; }
        .hud-title { color: #888; font-size: 0.8em; text-transform: uppercase; letter-spacing: 1px; margin-bottom: 10px; }
        .hud-value { font-size: 1.8em; font-weight: bold; }
        
        /* Network Diagram */
        .dashboard { display: flex; justify-content: center; align-items: stretch; max-width: 1400px; margin: 0 auto; gap: 20px; }
        .node { background: #1e1e1e; border: 2px solid #444; padding: 40px 20px; border-radius: 10px; width: 220px; display: flex; align-items: center; justify-content: center; text-align: center; font-size: 1.8em; font-weight: bold; box-shadow: 0 10px 20px rgba(0,0,0,0.5); z-index: 10; }
        .links-wrapper { display: flex; flex-direction: column; gap: 30px; flex-grow: 1; justify-content: center; }
        .link-container { display: flex; flex-direction: column; align-items: center; position: relative; width: 100%; }
        
        .headers { display: flex; flex-direction: column; align-items: center; margin-bottom: 8px; gap: 6px; }
        .band-label { font-weight: bold; font-size: 1.3em; color: #bbb; background: #1a1a1a; padding: 4px 16px; border-radius: 6px; border: 1px solid #333; }
        .vlan-badge { padding: 4px 12px; border-radius: 12px; font-size: 0.85em; font-weight: bold; text-transform: uppercase; letter-spacing: 1px; text-align: center; }
        .vlan-standby { background: #333; color: #888; border: 1px solid #555; }
        .vlan-active { background: #1b5e20; color: #a5d6a7; border: 1px solid #4CAF50; }
        .vlan-qos { background: #b71c1c; color: #ffcdd2; border: 1px solid #f44336; }

        .pipe-wrapper { display: flex; align-items: center; width: 100%; margin-bottom: 12px; }
        .connector-left, .connector-right { height: 6px; flex-grow: 1; background: #333; transition: all 0.3s ease; }
        .main-pipe { height: 10px; width: 80%; background: #333; border-radius: 5px; transition: all 0.3s ease; position: relative; overflow: hidden; }
        
        @keyframes flowRight { from { background-position: -40px 0; } to { background-position: 40px 0; } }
        .flow-green { background: repeating-linear-gradient(90deg, #4CAF50, #4CAF50 15px, #1b5e20 15px, #1b5e20 30px) !important; background-size: 60px 100% !important; animation: flowRight 0.6s linear infinite; box-shadow: 0 0 15px #4CAF50; }
        .flow-amber { background: repeating-linear-gradient(90deg, #FFC107, #FFC107 15px, #FF8F00 15px, #FF8F00 30px) !important; background-size: 60px 100% !important; animation: flowRight 0.8s linear infinite; box-shadow: 0 0 15px #FFC107; }
        .pipe-down { background: #F44336 !important; box-shadow: 0 0 15px #F44336; }
        
        .stats { display: flex; justify-content: space-around; width: 90%; background: #222; padding: 12px; border-radius: 8px; border: 1px solid #444; box-shadow: 0 4px 6px rgba(0,0,0,0.3); font-size: 0.9em; z-index: 5; }
        .stat-box { text-align: center; flex: 1; }
        .stat-label { color: #888; font-size: 0.75em; text-transform: uppercase; letter-spacing: 1px; }
        .stat-value { font-weight: bold; margin-top: 5px; font-size: 1.1em; font-family: monospace; }

        /* Graphing Section */
        .graphs-section { max-width: 1200px; margin: 50px auto 20px auto; background: #1e1e1e; padding: 20px; border-radius: 10px; border: 1px solid #444; box-shadow: 0 10px 20px rgba(0,0,0,0.5); }
        .tabs { display: flex; justify-content: center; gap: 15px; margin-bottom: 20px; }
        .tab-btn { background: #333; color: #fff; border: 1px solid #555; padding: 10px 20px; border-radius: 5px; cursor: pointer; font-weight: bold; transition: 0.3s; }
        .tab-btn:hover { background: #444; }
        .tab-btn.active { background: #4CAF50; border-color: #4CAF50; }
        .chart-container { position: relative; height: 300px; width: 100%; }
        
    </style>
</head>
<body>
    <h1>Rover Routing & Telemetry</h1>
    
    <div class="hud-container">
        <div class="hud-card" id="hud-quality-card">
            <div class="hud-title">Active Link Quality</div>
            <div class="hud-value" id="hud-quality">--</div>
        </div>
        <div class="hud-card">
            <div class="hud-title">Active Latency</div>
            <div class="hud-value" id="hud-latency">--</div>
        </div>
        <div class="hud-card">
            <div class="hud-title">Total Throughput</div>
            <div class="hud-value" id="hud-throughput">--</div>
        </div>
    </div>

    <div class="dashboard">
        <div class="node">MRDT<br>Rover</div>
        <div class="links-wrapper" id="links-container"></div>
        <div class="node">MRDT<br>Basestation</div>
    </div>

    <div class="graphs-section">
        <div class="tabs">
            <button class="tab-btn active" onclick="switchGraph('throughput')">Total Throughput</button>
            <button class="tab-btn" onclick="switchGraph('vlan')">VLAN Distribution</button>
            <button class="tab-btn" onclick="switchGraph('latency')">Latency (RTT)</button>
            <button class="tab-btn" onclick="switchGraph('etx')">ETX Route Cost</button>
        </div>
        <div class="chart-container">
            <canvas id="historyChart"></canvas>
        </div>
    </div>

    <script>
        const bands = ["5.8GHz", "2.4GHz", "900MHz"];
        const maxDataPoints = 60; // 60 seconds of history
        let currentGraphMode = 'throughput';
        
        // Setup historical data arrays
        const history = {
            labels: Array(maxDataPoints).fill(''),
            throughput: { "5.8GHz": Array(maxDataPoints).fill(0), "2.4GHz": Array(maxDataPoints).fill(0), "900MHz": Array(maxDataPoints).fill(0) },
            latency: { "5.8GHz": Array(maxDataPoints).fill(0), "2.4GHz": Array(maxDataPoints).fill(0), "900MHz": Array(maxDataPoints).fill(0) },
            etx: { "5.8GHz": Array(maxDataPoints).fill(0), "2.4GHz": Array(maxDataPoints).fill(0), "900MHz": Array(maxDataPoints).fill(0) },
            vlan: { "Telemetry (VLAN 2+3)": Array(maxDataPoints).fill(0), "Cameras (VLAN 4)": Array(maxDataPoints).fill(0) }
        };

        const colors = { 
            "5.8GHz": "#4CAF50", "2.4GHz": "#2196F3", "900MHz": "#9C27B0",
            "Telemetry (VLAN 2+3)": "#00BCD4", "Cameras (VLAN 4)": "#FF9800"
        };

        // Initialize Chart.js
        const ctx = document.getElementById('historyChart').getContext('2d');
        const chart = new Chart(ctx, {
            type: 'line',
            data: { labels: history.labels, datasets: [] },
            options: {
                responsive: true, maintainAspectRatio: false, animation: false,
                scales: { 
                    y: { beginAtZero: true, grid: { color: '#333' } },
                    x: { grid: { display: false }, ticks: { maxTicksLimit: 10 } }
                },
                plugins: { legend: { labels: { color: '#fff', font: { size: 14 } } } },
                color: '#fff'
            }
        });

        // Build DOM for links
        const container = document.getElementById('links-container');
        bands.forEach(band => {
            const safeId = band.replace('.', '');
            container.innerHTML += `
                <div class="link-container" id="container-${safeId}">
                    <div class="headers">
                        <div class="band-label" id="label-${safeId}">${band}</div>
                        <div class="vlan-badge vlan-standby" id="vlan-${safeId}">NO TRAFFIC</div>
                    </div>
                    <div class="pipe-wrapper">
                        <div class="connector-left" id="connL-${safeId}"></div>
                        <div class="main-pipe" id="pipe-${safeId}"></div>
                        <div class="connector-right" id="connR-${safeId}"></div>
                    </div>
                    <div class="stats">
                        <div class="stat-box"><div class="stat-label">Status</div><div class="stat-value" id="status-${safeId}">--</div></div>
                        <div class="stat-box"><div class="stat-label">ETX Cost</div><div class="stat-value" id="etx-${safeId}">--</div></div>
                        <div class="stat-box"><div class="stat-label">Latency</div><div class="stat-value" id="rtt-${safeId}">--</div></div>
                        <div class="stat-box"><div class="stat-label">Rx Mbps</div><div class="stat-value" id="rx-${safeId}">--</div></div>
                        <div class="stat-box"><div class="stat-label">Tx Mbps</div><div class="stat-value" id="tx-${safeId}">--</div></div>
                    </div>
                </div>
            `;
        });

        function switchGraph(mode) {
            currentGraphMode = mode;
            document.querySelectorAll('.tab-btn').forEach(btn => btn.classList.remove('active'));
            event.target.classList.add('active');
            updateChartDisplay();
        }

        function updateChartDisplay() {
            let datasets = [];
            
            // VLAN Graph needs different dataset mapping
            if (currentGraphMode === 'vlan') {
                datasets = ["Telemetry (VLAN 2+3)", "Cameras (VLAN 4)"].map(type => ({
                    label: type,
                    data: history.vlan[type],
                    borderColor: colors[type],
                    backgroundColor: colors[type] + '20',
                    borderWidth: 3,
                    pointRadius: 0,
                    fill: true,
                    tension: 0.3
                }));
            } else {
                datasets = bands.map(band => ({
                    label: band,
                    data: history[currentGraphMode][band],
                    borderColor: colors[band],
                    backgroundColor: colors[band] + '20',
                    borderWidth: 2,
                    pointRadius: 0,
                    fill: true,
                    tension: 0.3
                }));
            }
            
            chart.data.datasets = datasets;
            chart.update();
        }

        async function updateDashboard() {
            try {
                const res = await fetch('/api/stats');
                const data = await res.json();
                
                let activeLinkQuality = 'NO CONNECTION';
                let activeLatency = '--';
                let totalRx = 0;
                let totalTx = 0;
                let totalTelem = 0;
                let totalCam = 0;
                let qualityColor = '#F44336'; 

                history.labels.push(new Date().toLocaleTimeString([], {hour12: false, second: '2-digit'}));
                history.labels.shift();

                Object.keys(data).forEach(band => {
                    const safeId = band.replace('.', '');
                    const linkData = data[band];
                    
                    let rttVal = parseFloat(linkData.rtt) || 0;
                    let etxVal = parseFloat(linkData.etx) || 0;
                    let combinedThroughput = linkData.rx_mbps + linkData.tx_mbps;

                    // Update History Arrays
                    history.throughput[band].push(combinedThroughput);
                    history.throughput[band].shift();
                    history.latency[band].push(rttVal);
                    history.latency[band].shift();
                    history.etx[band].push(etxVal);
                    history.etx[band].shift();

                    // Sum metrics across all links (Babel ensures only one is heavily active anyway)
                    totalRx += linkData.rx_mbps;
                    totalTx += linkData.tx_mbps;
                    totalTelem += linkData.vlan_telem_mbps;
                    totalCam += linkData.vlan_cam_mbps;

                    if (linkData.active) {
                        activeLatency = linkData.rtt !== "N/A" ? `${linkData.rtt} ms` : '0 ms';
                        if (rttVal < 40) { activeLinkQuality = 'GOOD'; qualityColor = '#4CAF50'; }
                        else if (rttVal >= 40 && rttVal < 100) { activeLinkQuality = 'MODERATE'; qualityColor = '#FFC107'; }
                        else { activeLinkQuality = 'BAD'; qualityColor = '#FF5722'; }
                    }

                    // Update UI Elements
                    document.getElementById(`status-${safeId}`).innerText = linkData.status;
                    document.getElementById(`etx-${safeId}`).innerText = linkData.etx;
                    document.getElementById(`rtt-${safeId}`).innerText = linkData.rtt !== "N/A" ? linkData.rtt + ' ms' : '--';
                    document.getElementById(`rx-${safeId}`).innerText = linkData.rx_mbps.toFixed(2);
                    document.getElementById(`tx-${safeId}`).innerText = linkData.tx_mbps.toFixed(2);

                    const pipe = document.getElementById(`pipe-${safeId}`);
                    const connL = document.getElementById(`connL-${safeId}`);
                    const connR = document.getElementById(`connR-${safeId}`);
                    const vlanBadge = document.getElementById(`vlan-${safeId}`);
                    const label = document.getElementById(`label-${safeId}`);
                    
                    pipe.className = 'main-pipe'; connL.className = 'connector-left'; connR.className = 'connector-right';
                    let vlanText = 'STANDBY'; let vlanClass = 'vlan-standby'; let labelColor = '#bbb';

                    if (linkData.status === "DOWN") {
                        vlanText = 'LINK DOWN'; document.getElementById(`status-${safeId}`).style.color = '#F44336';
                        pipe.classList.add('pipe-down'); connL.classList.add('pipe-down'); connR.classList.add('pipe-down');
                    } else {
                        document.getElementById(`status-${safeId}`).style.color = '#fff';
                        if (linkData.active) {
                            let flowClass = 'flow-green'; labelColor = '#4CAF50';
                            if (rttVal >= 40) { flowClass = 'flow-amber'; labelColor = '#FFC107'; }
                            pipe.classList.add(flowClass); connL.classList.add(flowClass); connR.classList.add(flowClass);

                            if (band === "900MHz") {
                                vlanText = 'CARRYING: VLAN 2 & 3 | DROPPING: VLAN 4 (QoS ACTIVE)';
                                vlanClass = 'vlan-qos'; labelColor = '#f44336'; 
                            } else {
                                vlanText = 'FLOWING: VLAN 2 (Telem), VLAN 3 (Autonomy), VLAN 4 (Cam)';
                                vlanClass = 'vlan-active';
                            }
                        }
                    }

                    label.style.color = labelColor;
                    label.style.borderColor = linkData.active ? labelColor : '#333';
                    vlanBadge.className = `vlan-badge ${vlanClass}`;
                    vlanBadge.innerText = vlanText;
                });

                // Update VLAN History Arrays
                history.vlan["Telemetry (VLAN 2+3)"].push(totalTelem);
                history.vlan["Telemetry (VLAN 2+3)"].shift();
                history.vlan["Cameras (VLAN 4)"].push(totalCam);
                history.vlan["Cameras (VLAN 4)"].shift();

                // Update HUD
                document.getElementById('hud-quality').innerText = activeLinkQuality;
                document.getElementById('hud-quality').style.color = qualityColor;
                document.getElementById('hud-quality-card').style.borderColor = qualityColor;
                document.getElementById('hud-latency').innerText = activeLatency;
                document.getElementById('hud-throughput').innerText = (totalRx + totalTx).toFixed(2) + ' Mbps';

                updateChartDisplay();

            } catch (err) { console.error("Failed to fetch stats", err); }
        }

        setInterval(updateDashboard, 1000);
    </script>
</body>
</html>
"""

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=False)