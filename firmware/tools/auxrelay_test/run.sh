#!/bin/bash
# Host test of the AUX relay automation (schedule windows, override, event hysteresis).
#   firmware/tools/auxrelay_test/run.sh <path to ArduinoJson/src>
set -e
cd "$(dirname "$0")"
W=$(mktemp -d)
cp stubs/* test_auxrelay.cpp ../../PowerMeterRBAmp/src/auxrelay.cpp ../../PowerMeterRBAmp/src/auxrelay.h "$W/"
g++ -std=gnu++17 -I "$W" -I "${1:?path to ArduinoJson/src}" "$W/test_auxrelay.cpp" "$W/auxrelay.cpp" -o "$W/t"
"$W/t" | grep -v '^\[relay\]'
rm -rf "$W"
