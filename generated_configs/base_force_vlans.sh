#!/bin/bash
# Load the VLAN kernel module just in case
modprobe 8021q

# Force the base hardware interface UP
ip link set eth0 up

# Force create and configure Transit VLAN 99
ip link add link eth0 name eth0.99 type vlan id 99 2>/dev/null
ip addr add 10.99.2.2/30 dev eth0.99 2>/dev/null
ip link set eth0.99 up

# Force create and configure 900MHz Link (VLAN 900)
ip link add link eth0 name eth0.900 type vlan id 900 2>/dev/null
ip addr add 10.0.0.2/29 dev eth0.900 2>/dev/null
ip link set eth0.900 up

# Force create and configure 2.4GHz Link (VLAN 24)
ip link add link eth0 name eth0.24 type vlan id 24 2>/dev/null
ip addr add 10.0.0.10/29 dev eth0.24 2>/dev/null
ip link set eth0.24 up

