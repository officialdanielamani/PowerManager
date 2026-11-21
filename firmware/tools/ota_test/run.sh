#!/bin/bash
# otatag.h (firmware tag scanner + partition layout hash) on the PC.   firmware/tools/ota_test/run.sh
set -e
cd "$(dirname "$0")/../../.."
W=$(mktemp -d); trap 'rm -rf "$W"' EXIT
g++ -std=gnu++17 -Wall -I firmware/PowerMeterRBAmp/src firmware/tools/ota_test/test.cpp -o "$W/ota_test"
"$W/ota_test"
