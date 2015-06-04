#!/bin/bash

autophone_port=${AUTOPHONE_PORT:-28001}
autophone_ip=${AUTOPHONE_IP:-127.0.0.1}

if [[ -z "$1" ]]; then
    cmd="autophone-help"
else
    cmd="$@"
fi
echo $cmd | nc $autophone_ip $autophone_port

