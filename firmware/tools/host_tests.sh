#!/bin/bash
# Firmware checks that run on a PC (no ESP32, no arduino-cli):
#   1. files shared by both sketches are identical, generator templates match config.h
#   2. AUX relay automation (schedule / event / override) against the real auxrelay.cpp
#   3. hardware profile (rules, JSON, live / restart / confirm / rollback / crash loop) of hwprofile.cpp
#   4. MQTT link: the firmware's mqttlink.cpp against a real broker and the server's bridge
# Needs g++, OpenSSL headers (libssl-dev) and `pip install -r server/requirements-dev.txt`.
#   firmware/tools/host_tests.sh [path to ArduinoJson/src]
set -e
cd "$(dirname "$0")/../.."
PY=${PYTHON:-python3}
AJ=${1:-$ARDUINOJSON_SRC}
if [ -z "$AJ" ]; then   # fetch the library once
  AJ=firmware/tools/.cache/ArduinoJson/src
  [ -d "$AJ" ] || git clone --quiet --depth 1 -b "v${ARDUINOJSON_VERSION:-7.1.0}" \
    https://github.com/bblanchon/ArduinoJson.git firmware/tools/.cache/ArduinoJson
fi
AJ=$(cd "$AJ" && pwd)
$PY firmware/tools/check_shared.py
bash firmware/tools/auxrelay_test/run.sh "$AJ"
bash firmware/tools/hwprofile_test/run.sh "$AJ"
$PY firmware/tools/mqttlink_test/run.py "$AJ"
