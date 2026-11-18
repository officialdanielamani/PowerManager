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
          "i2cbus.cpp", "i2cbus.h", "relays.h", "auxrelay.cpp", "auxrelay.h", "sessstats.h", "mqttlink.cpp", "mqttlink.h", "ups.cpp", "ups.h", "sensorcheck.h", "ota.cpp", "ota.h", "otatag.h"]
# Files that differ on purpose (sensor, relays, sessions): compare by hand when changing them.
DIFFERENT = ["app_state.h", "app_state.cpp", "control.cpp", "control.h", "storage.cpp", "storage.h",
             "net.cpp", "web.cpp", "webui.h", "led.cpp", "hwmap.h", "relaymap.h", "relays.cpp"]
# rbAmp only for now (hardware profile from the server, docs/protocol.md §12): hwprofile.h / hwprofile.cpp

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
# The Arduino upload hard-codes boot_app0 at 0xe000 and the app at 0x10000 (platform.txt of the ESP32 core): a table that moves
# them flashes fine but never boots ("No bootable app partitions in the partition table").
def check_partitions(path):
    rows = {}
    for line in path.read_text().splitlines():
        line = line.split("#")[0].strip()
        if not line:
            continue
        name, typ, sub, off, size = [x.strip() for x in line.split(",")][:5]
        rows[name] = (typ, sub, int(off, 0), int(size, 0))
    errs = []
    want = {"nvs": 0x9000, "otadata": 0xE000, "app0": 0x10000}
    for name, off in want.items():
        if name not in rows or rows[name][2] != off:
            errs.append(f"{name} must be at {off:#x} (Arduino upload offsets)")
    spans = sorted((o, o + sz, n) for n, (t, st, o, sz) in rows.items())
    for (a0, a1, an), (b0, b1, bn) in zip(spans, spans[1:]):
        if a1 > b0:
            errs.append(f"{an} overlaps {bn}")
    for n, (t, st, o, sz) in rows.items():
        if t == "app" and (o % 0x10000 or sz % 0x1000):
            errs.append(f"{n}: app partitions start on a 64 KB boundary")
    if spans and spans[-1][1] > 16 * 1024 * 1024:
        errs.append("table is larger than 16 MB flash")
    return errs

for sk in ("PowerMeterPZEM", "PowerMeterRBAmp"):
    for e in check_partitions(A.parent.parent / sk / "partitions.csv"):
        print(f"PARTITIONS {sk}: {e}")
        bad.append("partitions.csv")
# Files of one sketch must not be in the other (a merged copy breaks the Arduino build: every .cpp in src/ is compiled).
for sk, foreign in (("PowerMeterRBAmp", ("sensor_pzem.cpp", "sensor_pzem.h")), ("PowerMeterPZEM", ("rbamp.cpp", "rbamp.h", "hwprofile.cpp", "hwprofile.h"))):
    for f in foreign:
        if (A.parent.parent / sk / "src" / f).exists():
            print(f"FOREIGN FILE: {sk}/src/{f} belongs to the other sketch")
            bad.append(f)
print("shared files identical" if not bad else f"{len(bad)} shared file(s) differ")
print("differ on purpose (check by hand): " + ", ".join(DIFFERENT))
sys.exit(1 if bad else 0)
