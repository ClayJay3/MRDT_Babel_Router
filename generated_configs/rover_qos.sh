#!/bin/bash
# MRDT QoS - HTB shaper on each wireless uplink.
# Shaping just under the radio's real rate moves the queue onto the Pi
# (where we control it) instead of the radio, so prioritisation works
# and bufferbloat stays low. Bands: control > high-priority > bulk.
set -u
sysctl -w net.ipv4.ip_forward=1

# Kernel modules (Raspberry Pi OS / Pi 5 do not autoload these)
modprobe sch_htb 2>/dev/null
modprobe sch_fq_codel 2>/dev/null
modprobe cls_fw 2>/dev/null
modprobe cls_u32 2>/dev/null

# Mark Babel control traffic (UDP 6696) so it always rides the top class.
# Babel's transport is IPv6 link-local, so the ip6tables rule is the key one.
iptables  -t mangle -D POSTROUTING -p udp --dport 6696 -j MARK --set-mark 10 2>/dev/null
iptables  -t mangle -A POSTROUTING -p udp --dport 6696 -j MARK --set-mark 10
ip6tables -t mangle -D POSTROUTING -p udp --dport 6696 -j MARK --set-mark 10 2>/dev/null
ip6tables -t mangle -A POSTROUTING -p udp --dport 6696 -j MARK --set-mark 10

# Usable rate per link in kbit (~90% of real radio throughput)
declare -A RATE
RATE["eth0.24"]=27000
RATE["eth0.900"]=9000

for i in eth0.24 eth0.900; do
    R=${RATE[$i]}
    tc qdisc del dev $i root 2>/dev/null
    tc qdisc add dev $i root handle 1: htb default 30
    tc class add dev $i parent 1:  classid 1:1  htb rate ${R}kbit ceil ${R}kbit

    # 1:10 control plane (Babel)   1:20 high priority   1:30 bulk/default
    tc class add dev $i parent 1:1 classid 1:10 htb rate $((R*10/100))kbit ceil ${R}kbit prio 0
    tc class add dev $i parent 1:1 classid 1:20 htb rate $((R*50/100))kbit ceil ${R}kbit prio 1
    tc class add dev $i parent 1:1 classid 1:30 htb rate $((R*40/100))kbit ceil ${R}kbit prio 2

    # fq_codel leaves keep latency low inside each class
    tc qdisc add dev $i parent 1:10 handle 10: fq_codel
    tc qdisc add dev $i parent 1:20 handle 20: fq_codel
    tc qdisc add dev $i parent 1:30 handle 30: fq_codel

    # Babel control (fwmark 10, set above) -> top class, IPv4 and IPv6
    tc filter add dev $i parent 1:0 protocol ip   prio 1 handle 10 fw flowid 1:10
    tc filter add dev $i parent 1:0 protocol ipv6 prio 2 handle 10 fw flowid 1:10

    # High priority (telemetry / motors)
    tc filter add dev $i parent 1:0 protocol ip prio 3 u32 match ip src 192.168.2.0/24 flowid 1:20
    tc filter add dev $i parent 1:0 protocol ip prio 3 u32 match ip dst 192.168.2.0/24 flowid 1:20
    tc filter add dev $i parent 1:0 protocol ip prio 3 u32 match ip src 192.168.3.0/24 flowid 1:20
    tc filter add dev $i parent 1:0 protocol ip prio 3 u32 match ip dst 192.168.3.0/24 flowid 1:20

    # Bulk (cameras / science) - also the default class
    tc filter add dev $i parent 1:0 protocol ip prio 4 u32 match ip src 192.168.4.0/24 flowid 1:30
    tc filter add dev $i parent 1:0 protocol ip prio 4 u32 match ip dst 192.168.4.0/24 flowid 1:30
done
