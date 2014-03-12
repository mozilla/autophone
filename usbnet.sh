#!/bin/bash

# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.

help() {
    cat<<EOF
usage: usbnet.sh -d host_eth_device -s phone_serial_no -p phone_ppp_ip -P phone_ip -h host_ppp_ip -r usb_network

host_eth_device - host ethernet device.
phone_serial_no - adb serial number of phone.
phone_ppp_ip    - ip address to be assigned phone's ppp device.
phone_ip        - phone ip address over wifi.
host_ppp_ip     - ip address to be assigned host's ppp device.
usb_network     - network mask to be used to create routes on phone.

EOF
    exit 2
}

get_ppp() {
    local ppp
    ppp=$(ip address list | grep  $phone_ppp_ip | tail -1 | sed 's|.*\(ppp[0-9]*\)|\1|')
    echo $ppp
}

wait_for_device() {
   local state wait_time sleep_time max_wait
   let wait_time=0
   let sleep_time=30
   let max_wait=120
   echo "waiting for device $serialno"
   while [[ $wait_time -lt $max_wait ]]; do
       state=$(adb -s $serialno get-state)
       if [[ "$state" == "device" ]]; then
           return 0
       fi
       sleep $sleep_time
       let wait_time=wait_time+sleep_time
   done
   echo "Device $serialno not found"
   exit 1
}

run_adb_as_root() {
  local result
  local success1="adbd is already running as root"
  local success2="restarting adbd as root"
  result=$(adb -s $serialno root)
  if [[ "$result" == "$success1" || \
        "$result" == "$success2" ]]; then
    return 0
  fi
  cat<<EOF
FATAL ERROR

ADB can not be run as root on device $serialno.

USB Netwoking is not available on this device.
EOF
  exit 1
}
options="d:s:p:P:h:r:"

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
            # phone ppp ip
            phone_ppp_ip=$OPTARG
            ;;
        P)
            # phone ip
            phone_ip=$OPTARG
            ;;
        h)
            # host ppp ip
            host_ppp_ip=$OPTARG
            ;;
        r)  # ip address or network address for usbnet destinations.
            destination_net=$OPTARG
            ;;
    esac
done

if [[ -z "$host_device" || -z "$serialno" || -z "$phone_ppp_ip" || -z "$phone_ip" || -z "$host_ppp_ip" ]]; then
    help
fi

wait_for_device
run_adb_as_root
wait_for_device
sleep 10

if [[ "$(sysctl net.ipv4.ip_forward)" != "net.ipv4.ip_forward = 1" ]]; then
    echo "turning on ip forwarding"
    sudo sysctl net.ipv4.ip_forward=1
fi

ppp=$(get_ppp)

if [[ -z "$ppp" ]]; then

    echo "creating ppp"

    sudo -i adb -s $serialno ppp "shell:pppd nodetach noauth noipdefault nobsdcomp nodeflate defaultroute /dev/tty" \
        nodetach noauth noipdefault nodeflate nobsdcomp notty $host_ppp_ip:$phone_ppp_ip

    sleep 10

    if [[ -n "$destination_net" ]]; then
        # Set the phone's route to use the usbnet host as gateway for the the destination_net.
        adb -s $serialno shell "ip route change $destination_net dev ppp0"
        adb -s $serialno shell "ip route change default via $host_ppp_ip dev ppp0"
        adb -s $serialno shell "ip route"
    fi
fi

if ! sudo iptables -t nat -S POSTROUTING | grep -q $phone_ppp_ip; then

    ppp=$(get_ppp)

    if [[ -z "$ppp" ]]; then
        echo "unable to get ppp device"
        exit 1
    fi
    echo "creating $ppp for $serialno phone $phone_ppp_ip, host $host_ppp_ip"

    echo "setting up nat forwarding for $ppp phone: $phone_ppp_ip, host: $host_device"
    sudo iptables -t nat -A POSTROUTING -s $phone_ppp_ip -j MASQUERADE -o $host_device
    sudo iptables -A FORWARD --in-interface $ppp -j ACCEPT
    sudo iptables -A INPUT --in-interface $ppp -j ACCEPT

fi

sudo ip route add $phone_ip via $host_ppp_ip
