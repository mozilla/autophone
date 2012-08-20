#!/bin/bash

# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.

# Simple smoketest. Launches autophone with a fresh cache file and waits
# until the user restarts a connected phone. It then runs a simple test
# that just verifies that fennec can be installed and launched, and reports
# the result.

cleanup() {
    if [ $cache ] && [ -a $cache ]
    then
        rm $cache
    fi
    kill %1
    sleep 1
    exit
}

trap cleanup INT TERM EXIT
cache=`mktemp`
rm -f smoketest_pass smoketest_fail
echo Launching autophone...
python autophone.py --cache $cache -t tests/smoketest_manifest.ini &
sleep 1
echo Please restart the agent/phone for smoke test...
i=0
while [ $i -le 60 ]
do
    if [ -s $cache ]
    then
        break
    fi
    sleep 5
done

if [ -s $cache ]
then
    echo Detected phone. Proceeding with test...
else
    echo Failed to detect phone.
    exit 1
fi

echo Triggering run...
python trigger_runs.py latest
if [ $? -ne 0 ]
then
    echo 'Could not find a suitable build!'
    exit 1
fi

echo 'Waiting for result...'
i=0
while [ $i -le 60 ]
do
    i=$(($i+1))
    if [ -a smoketest_pass ]
    then
        echo 'Smoke test passed!'
        break
    fi
    if [ -a smoketest_fail ]
    then
        echo 'Smoke test failed!'
        break
    fi
    sleep 5
done
