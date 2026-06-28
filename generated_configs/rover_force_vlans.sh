#!/bin/bash
# MRDT brute-force VLAN bring-up. Run at boot via the matching
# *_force_vlans.service. See README for the NetworkManager unmanaged rule.
modprobe 8021q
ip link set eth0 up

# Transit VLAN 99 (OSPF to the local Cisco switch)
ip link add link eth0 name eth0.99 type vlan id 99 2>/dev/null
ip addr add 10.99.1.2/30 dev eth0.99 2>/dev/null
ip link set eth0.99 up

# 2.4GHz link (VLAN 24)
ip link add link eth0 name eth0.24 type vlan id 24 2>/dev/null
ip addr add 10.0.0.9/29 dev eth0.24 2>/dev/null
ip link set eth0.24 up

# 900MHz link (VLAN 900)
ip link add link eth0 name eth0.900 type vlan id 900 2>/dev/null
ip addr add 10.0.0.1/29 dev eth0.900 2>/dev/null
ip link set eth0.900 up
