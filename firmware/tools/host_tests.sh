#!/bin/bash
# Firmware checks that run on a PC (no ESP32, no arduino-cli):
#   1. files shared by both sketches are identical, generator templates match config.h
#   2. AUX relay automation (schedule / event / override) against the real auxrelay.cpp
#   3. hardware profile (rules, JSON, live / restart / confirm / rollback / crash loop) of hwprofile.cpp
#   4. ESP32 sources against stubs (syntax / static_asserts of the hardware tables, command code of net.cpp, .ino glue)
#   5. sensor check collector + zero / tare helpers (src/sensorcheck.h, shared by both sketches)
#   6. I2C layer against fakes (relay board unplugged / power loss / plugged in later; stuck bus freed)
#   7. OTA firmware tag scanner + partition layout hash (src/otatag.h)
#   8. MQTT link: the firmware's mqttlink.cpp against a real broker and the server's bridge
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
bash firmware/tools/syntax_check/run.sh "$AJ"
bash firmware/tools/sensorcheck_test/run.sh
bash firmware/tools/ota_test/run.sh
TAIL=1 bash firmware/tools/i2c_test/run.sh
$PY firmware/tools/mqttlink_test/run.py "$AJ"
