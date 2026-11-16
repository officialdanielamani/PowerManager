#!/bin/bash
# Compile a sketch with the reference board settings (ESP32S3 Dev Module, USB CDC on boot, 4 MB flash,
# Minimal SPIFFS partition scheme). Needs arduino-cli + firmware/tools/setup_arduino.sh once.
#   firmware/tools/build.sh PowerMeterRBAmp            build firmware/PowerMeterRBAmp
#   firmware/tools/build.sh PowerMeterPZEM dc          PZEM sketch as the DC meter (METER_DC 1)
#   firmware/tools/build.sh PowerMeterRBAmp example    with examples/config_rbamp_2xUI5_MCP23017.h
# The .bin files are left in firmware/build/<name>/.
set -e
cd "$(dirname "$0")/.."
CLI=${ARDUINO_CLI:-arduino-cli}
SKETCH=${1:?sketch folder: PowerMeterPZEM or PowerMeterRBAmp}
VARIANT=${2:-default}
SRC=$SKETCH
if [ "$VARIANT" != default ]; then      # variants are built from a copy, the sketch itself is not touched
  SRC=build/src-$SKETCH-$VARIANT/$SKETCH
  rm -rf "build/src-$SKETCH-$VARIANT" && mkdir -p "$(dirname "$SRC")" && cp -r "$SKETCH" "$SRC"
  case "$VARIANT" in
    dc) sed -i 's/^#define METER_DC 0/#define METER_DC 1/' "$SRC/config.h" ;;
    ws-only) sed -i 's/^#define NET_MQTT 1/#define NET_MQTT 0/' "$SRC/config.h" ;;
    mqtt-only) sed -i 's/^#define NET_WEBSOCKET 1/#define NET_WEBSOCKET 0/' "$SRC/config.h" ;;
    all-parts) sed -i -E 's/^#define (HAS_DS3231|UPS_ENABLE|BATTERY_ENABLE|TEMP_DS18B20|TEMP_SHT) 0/#define \1 1/' "$SRC/config.h" ;;
    example) cp examples/config_rbamp_2xUI5_MCP23017.h "$SRC/config.h" ;;
    *) echo "unknown variant '$VARIANT' (dc, ws-only, mqtt-only, all-parts, example)"; exit 2 ;;
  esac
fi
OUT=build/$SKETCH-$VARIANT
$CLI compile -b esp32:esp32:esp32s3:CDCOnBoot=cdc,FlashSize=4M,PartitionScheme=min_spiffs \
  --build-path "$OUT" "$SRC"
echo "built: firmware/$OUT"
