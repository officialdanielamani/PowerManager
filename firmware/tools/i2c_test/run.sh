#!/bin/bash
# Host tests of the I2C layer against fakes: relays.cpp (a relay board that is unplugged / loses power / is plugged in later
# works again by itself) and i2cbus.cpp (a bus held low by a device is freed; a missing device is not a bus fault).
#   firmware/tools/i2c_test/run.sh
set -e
cd "$(dirname "$0")"
SRC=../../PowerMeterRBAmp/src
W=$(mktemp -d)
trap 'rm -rf "$W"' EXIT
FLAGS="-std=gnu++17 -Wall -Wno-unused-function"
# relays.cpp (copied next to the stubs, so its "relaymap.h" / "i2cbus.h" are the fakes) with fake expanders
for sketch in PowerMeterRBAmp PowerMeterPZEM; do
  rm -rf "$W/r"; mkdir -p "$W/r"
  cp relay_stubs/* "$W/r/"; cp ../../$sketch/src/relays.cpp ../../$sketch/src/relays.h "$W/r/"
  g++ $FLAGS -I "$W/r" -I stubs test_relays.cpp "$W/r/relays.cpp" -o "$W/relays"
  echo "== $sketch relays.cpp"; "$W/relays" | tail -${TAIL:-9}
done
# i2cbus.cpp (the real file, with a fake config.h) against a fake Wire + GPIO
rm -rf "$W/b"; mkdir -p "$W/b/src"
cp ../../PowerMeterRBAmp/src/i2cbus.cpp ../../PowerMeterRBAmp/src/i2cbus.h "$W/b/src/"; cp stubs/config.h "$W/b/config.h"
g++ $FLAGS -I "$W/b/src" -I stubs test_bus.cpp "$W/b/src/i2cbus.cpp" -o "$W/bus"
echo "== i2cbus.cpp"; "$W/bus"
diff -q ../../PowerMeterRBAmp/src/i2cbus.cpp ../../PowerMeterPZEM/src/i2cbus.cpp >/dev/null || { echo "i2cbus.cpp differs between the sketches"; exit 1; }
