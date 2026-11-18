#!/bin/bash
# ESP32 sources that the PC can check without arduino-cli: a syntax / type check (g++ -fsyntax-only) against small
# Arduino stubs, so the compile-time rules of the hardware tables (static_asserts in hwmap.h / relaymap.h) are
# exercised, and the hardware-profile code of net.cpp / the .ino is compiled as extracted snippets.
#   - rbamp.cpp, relays.cpp, i2cbus.cpp (rbAmp) and relays.cpp (PZEM) with their config.h,
#     rbAmp also with firmware/examples/*.h as config.h
#   - a CHANNELS table that uses a relay output twice must FAIL to compile
#   - the hw_profile / i2c_scan / rbamp_addr command code of net.cpp and the profile glue of PowerMeterRBAmp.ino
# What it cannot do: link, or compile net.cpp / web.cpp / mesh.cpp / mqttlink.cpp (Wi-Fi, WebSockets ...). The real
# build is firmware/tools/build.sh (arduino-cli, run by CI).
#   firmware/tools/syntax_check/run.sh [path to ArduinoJson/src]
set -e
cd "$(dirname "$0")/../../.."
AJ=${1:-$ARDUINOJSON_SRC}
if [ -z "$AJ" ]; then   # fetch the library once (same cache as host_tests.sh)
  AJ=firmware/tools/.cache/ArduinoJson/src
  [ -d "$AJ" ] || git clone --quiet --depth 1 -b "v${ARDUINOJSON_VERSION:-7.1.0}" \
    https://github.com/bblanchon/ArduinoJson.git firmware/tools/.cache/ArduinoJson
fi
AJ=$(cd "$AJ" && pwd)
ROOT=$(pwd)
STUBS=$ROOT/firmware/tools/syntax_check/stubs
W=$(mktemp -d)
trap 'rm -rf "$W"' EXIT
CXX="g++ -std=gnu++17 -fsyntax-only -Wall -Wno-unused-function -I $STUBS -I $AJ"
fail=0

check() {  # check <label> <include dir with config.h and src/> <source files...>
  local label=$1 dir=$2; shift 2
  local bad=0
  for f in "$@"; do
    if ! out=$($CXX -I "$dir" "$dir/src/$f" 2>&1); then
      echo "FAIL  $label: $f"; echo "$out" | grep -E "error" | head -5; fail=1; bad=1
    fi
  done
  [ $bad = 0 ] && echo "ok    $label: $*" || true
}

# ---- default config.h of both sketches
check "rbAmp config.h" firmware/PowerMeterRBAmp rbamp.cpp relays.cpp i2cbus.cpp control.cpp app_state.cpp storage.cpp battery.cpp temp.cpp ups.cpp auxrelay.cpp
check "PZEM config.h" firmware/PowerMeterPZEM relays.cpp i2cbus.cpp control.cpp app_state.cpp storage.cpp battery.cpp temp.cpp ups.cpp auxrelay.cpp sensor_pzem.cpp

# ---- every example config as the rbAmp config.h
for ex in firmware/examples/config_rbamp_*.h; do
  rm -rf "$W/ex"; mkdir -p "$W/ex"
  cp -r firmware/PowerMeterRBAmp/src "$W/ex/src"; cp "$ex" "$W/ex/config.h"
  check "rbAmp + $(basename "$ex")" "$W/ex" rbamp.cpp relays.cpp
done

# ---- the compile-time rules must reject a broken table (same relay output twice)
rm -rf "$W/neg"; mkdir -p "$W/neg"
cp -r firmware/PowerMeterRBAmp/src "$W/neg/src"; cp firmware/PowerMeterRBAmp/config.h "$W/neg/config.h"
python3 - "$W/neg/config.h" <<'PY'
import re, sys
p = sys.argv[1]
s = open(p).read()
s2 = re.sub(r"(\{\s*1,\s+2,\s+SCT_\w+,\s+1,\s+)2( \})", r"\g<1>1\2", s, count=1)  # CH2 -> relay board 1, output 1
assert s2 != s, "CHANNELS format changed: update this check"
open(p, "w").write(s2)
PY
if $CXX -I "$W/neg" "$W/neg/src/rbamp.cpp" >/dev/null 2>&1; then
  echo "FAIL  a CHANNELS table with a relay output used twice was accepted"; fail=1
else
  echo "ok    a CHANNELS table with a relay output used twice is rejected"
fi

# ---- command code of net.cpp and the profile glue of the .ino, compiled as snippets
python3 firmware/tools/syntax_check/snippets.py "$W"
for f in net_snippet glue_snippet; do
  if out=$($CXX -I "$ROOT/firmware/PowerMeterRBAmp/src" -I "$ROOT/firmware/PowerMeterRBAmp" "$W/$f.cpp" 2>&1); then
    echo "ok    $f (code extracted from the sketch)"
  else
    echo "FAIL  $f"; echo "$out" | grep -E "error" | head -5; fail=1
  fi
done

[ $fail = 0 ] && echo "ESP32 syntax checks passed" || { echo "ESP32 syntax checks FAILED"; exit 1; }
