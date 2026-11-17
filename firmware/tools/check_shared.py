#!/usr/bin/env python3
"""The rbAmp sketch (PowerMeterRBAmp, multi-channel) is a separate firmware but shares most of its code with
the PZEM sketch (PowerMeterPZEM, AC / DC). Arduino IDE cannot share files between sketches, so they are copies:
this script lists shared files that differ, so a fix in one is not forgotten in the other.

    python3 firmware/tools/check_shared.py        (exit code 1 if any shared file differs)
"""
import filecmp
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
A, B = ROOT / "PowerMeterPZEM" / "src", ROOT / "PowerMeterRBAmp" / "src"
SHARED = ["outbox.cpp", "outbox.h", "mesh.cpp", "mesh.h", "battery.cpp", "battery.h", "certs.h",
          "system.cpp", "system.h", "led.h", "net.h", "web.h", "timekeep.h", "timekeep.cpp", "temp.cpp", "temp.h",
          "i2cbus.cpp", "i2cbus.h", "relaymap.h", "relays.cpp", "relays.h", "auxrelay.cpp", "auxrelay.h", "sessstats.h", "mqttlink.cpp", "mqttlink.h", "ups.cpp", "ups.h"]
# Files that differ on purpose (sensor, relays, sessions): compare by hand when changing them.
DIFFERENT = ["app_state.h", "app_state.cpp", "control.cpp", "control.h", "storage.cpp", "storage.h",
             "net.cpp", "web.cpp", "webui.h", "led.cpp", "hwmap.h"]

bad = [f for f in SHARED if not filecmp.cmp(A / f, B / f, shallow=False)]
for f in bad:
    print(f"DIFFERS: {f}  (PowerMeterPZEM/src vs PowerMeterRBAmp/src)")
# The server's config.h generator works on copies of the two config.h files.
GEN = A.parent.parent.parent / "server" / "app" / "firmware_cfg"
for sketch, name in ((A, "pzem_config.h"), (B, "rbamp_config.h")):
    if GEN.exists() and not filecmp.cmp(sketch.parent / "config.h", GEN / name, shallow=False):
        print(f"DIFFERS: {sketch.parent.name}/config.h vs server/app/firmware_cfg/{name}  (copy it over)")
        bad.append(name)
# The custom partition table (16 MB flash) is the same for both sketches.
if not filecmp.cmp(A.parent / "partitions.csv", B.parent / "partitions.csv", shallow=False):
    print("DIFFERS: partitions.csv  (PowerMeterPZEM vs PowerMeterRBAmp)")
    bad.append("partitions.csv")
print("shared files identical" if not bad else f"{len(bad)} shared file(s) differ")
print("differ on purpose (check by hand): " + ", ".join(DIFFERENT))
sys.exit(1 if bad else 0)
