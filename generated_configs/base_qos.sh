#!/bin/bash
# Ensure the Linux Kernel is routing packets
sysctl -w net.ipv4.ip_forward=1

# Mark Babel Control Traffic (UDP 6696) as VIP
# We delete first to prevent duplicate rules if the script is run multiple times
iptables -t mangle -D POSTROUTING -p udp --dport 6696 -j MARK --set-mark 10 2>/dev/null
iptables -t mangle -A POSTROUTING -p udp --dport 6696 -j MARK --set-mark 10

ip6tables -t mangle -D POSTROUTING -p udp --dport 6696 -j MARK --set-mark 10 2>/dev/null
ip6tables -t mangle -A POSTROUTING -p udp --dport 6696 -j MARK --set-mark 10

# Apply Traffic Control (QoS) to all wireless subinterfaces
for i in eth0.900 eth0.24; do
    tc qdisc del dev $i root 2>/dev/null
    tc qdisc add dev $i root handle 1: prio bands 3

    # BAND 1 (HIGHEST PRIORITY - Babel Protocol & Telemetry/Motors)
    tc filter add dev $i protocol ip parent 1:0 prio 1 handle 10 fw flowid 1:1
    tc filter add dev $i protocol ipv6 parent 1:0 prio 1 handle 10 fw flowid 1:1
    tc filter add dev $i protocol ip parent 1: prio 1 u32 match ip dst 192.168.2.0/24 flowid 1:1
    tc filter add dev $i protocol ip parent 1: prio 1 u32 match ip dst 192.168.3.0/24 flowid 1:1

    # BAND 3 (LOWEST PRIORITY - Cameras/Science - Dropped if congested)
    tc filter add dev $i protocol ip parent 1: prio 3 u32 match ip dst 192.168.4.0/24 flowid 1:3
done
