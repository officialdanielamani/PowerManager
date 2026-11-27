#!/bin/bash
# Host test of the sensor check collector and the zero / tare helpers (src/sensorcheck.h, shared by both sketches).
#   firmware/tools/sensorcheck_test/run.sh
set -e
cd "$(dirname "$0")"
W=$(mktemp -d)
trap 'rm -rf "$W"' EXIT
g++ -std=gnu++17 -Wall -I ../../PowerMeterRBAmp/src test_sensorcheck.cpp -o "$W/t"
"$W/t"
cmp ../../PowerMeterRBAmp/src/sensorcheck.h ../../PowerMeterPZEM/src/sensorcheck.h || { echo "sensorcheck.h differs between the sketches"; exit 1; }
