#!/bin/bash

# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.

help() {
    cat<<EOF
usage: usbnet.sh -d host_eth_device -s phone_serial_no -p phone_id -h host_ip -r usb_network
EOF
    exit 2
}

getppp() {
    local ppp
    ppp=$(ip address list | grep  $phone_ip | tail -1 | sed 's|.*\(ppp[0-9]*\)|\1|')
    echo $ppp
}

options="d:s:p:h:r:"

while getopts $options optname; do
    case $optname in
        d)
            # host ethernet device
            host_device=$OPTARG
            ;;
        s)
            # device serialnumber
            serialno=$OPTARG
            ;;
        p)
            # local/phone ip
            phone_ip=$OPTARG
            ;;
        h)
            # remote/host ip
            host_ip=$OPTARG
            ;;
        r)  # ip address or network address for usbnet destinations.
            destination_net=$OPTARG
            ;;
    esac
done

if [[ -z "$host_device" || -z "$serialno" || -z "$phone_ip" || -z "$host_ip" ]]; then
    help
fi

echo "waiting for device $serialno"
sudo -i adb -s $serialno wait-for-device

sleep 10

#adb -s $serialno shell "svc wifi disable"

if [[ "$(sysctl net.ipv4.ip_forward)" != "net.ipv4.ip_forward = 1" ]]; then
    echo "turning on ip forwarding"
    sudo sysctl net.ipv4.ip_forward=1
fi

ppp=$(getppp)

if [[ -z "$ppp" ]]; then

    echo "creating ppp"

    sudo -i adb -s $serialno ppp "shell:pppd nodetach noauth noipdefault defaultroute /dev/tty" \
        nodetach noauth noipdefault notty $host_ip:$phone_ip

    sleep 10

    if [[ -n "$destination_net" ]]; then
        # Set the phone's route to use the usbnet host as gateway for the the destination_net.
        sudo -i adb -s $serialno shell "ip route add $destination_net via $host_ip dev ppp0"
    fi
fi

if ! sudo iptables -t nat -S POSTROUTING | grep -q $phone_ip; then

    ppp=$(getppp)

    if [[ -z "$ppp" ]]; then
        echo "unable to get ppp device"
        exit 1
    fi
    echo "creating $ppp for $serialno phone $phone_ip, host $host_ip"

    echo "setting up nat forwarding for $ppp phone: $phone_ip, host: $host_device"
    sudo iptables -t nat -A POSTROUTING -s $phone_ip -j MASQUERADE -o $host_device
    sudo iptables -A FORWARD --in-interface $ppp -j ACCEPT
    sudo iptables -A INPUT --in-interface $ppp -j ACCEPT
fi
