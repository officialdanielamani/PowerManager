#!/bin/bash
# Host test of the hardware profile logic (rules, JSON, live / reboot / confirm / rollback / crash loop).
#   firmware/tools/hwprofile_test/run.sh <path to ArduinoJson/src>
set -e
cd "$(dirname "$0")"
W=$(mktemp -d)
cp stubs/* test_hwprofile.cpp ../../PowerMeterRBAmp/src/hwprofile.cpp ../../PowerMeterRBAmp/src/hwprofile.h "$W/"
g++ -std=gnu++17 -Wall -I "$W" -I "${1:?path to ArduinoJson/src}" "$W/test_hwprofile.cpp" "$W/hwprofile.cpp" -o "$W/t"
"$W/t" | grep -v '^\[hwp\]'
rm -rf "$W"
