#!/bin/bash
# Install the board package and libraries the firmware is built with (arduino-cli must be on the PATH).
#   firmware/tools/setup_arduino.sh
set -e
CLI=${ARDUINO_CLI:-arduino-cli}
URL=https://espressif.github.io/arduino-esp32/package_esp32_index.json
$CLI core update-index --additional-urls "$URL"
$CLI core install "esp32:esp32@${ESP32_CORE:-3.3.5}" --additional-urls "$URL"
$CLI lib install "ArduinoJson@${ARDUINOJSON_VERSION:-7.1.0}" "WebSockets@${WEBSOCKETS_VERSION:-2.7.2}"
