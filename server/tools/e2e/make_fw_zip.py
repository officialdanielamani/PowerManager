#!/usr/bin/env python3
"""A fake Arduino IDE export (zip) of a PowerManager firmware for the browser test.   make_fw_zip.py <out.zip> [version]
The partition table is the one of firmware/PowerMeterRBAmp/partitions.csv, so its layout hash matches the simulated meter."""
import io
import struct
import sys
import zipfile

out = sys.argv[1]
ver = sys.argv[2] if len(sys.argv) > 2 else "1.3.0"
entries = [(1, 2, 0x9000, 0x5000), (1, 0, 0xE000, 0x2000), (0, 0x10, 0x10000, 0x600000),
           (0, 0x11, 0x610000, 0x600000), (1, 0x82, 0xC10000, 0x3E0000), (1, 3, 0xFF0000, 0x10000)]
table = b"".join(struct.pack("<HBBII16sI", 0x50AA, t, st, off, size, b"x", 0) for t, st, off, size in entries) + b"\xff" * 32
app = b"\xe9" + b"\x01" * 100 + b"PMFW" b"TAG|rbamp-mc|" + ver.encode() + b"|esp32s3-n16r8|" + b"\x02" * 200000
with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
    z.writestr("PowerMeterRBAmp.ino.bin", app)
    z.writestr("PowerMeterRBAmp.ino.bootloader.bin", b"\xe9boot" * 50)
    z.writestr("PowerMeterRBAmp.ino.partitions.bin", table)
    z.writestr("boot_app0.bin", b"\x01" * 64)
    z.writestr("build.options.json", '{"fqbn": "esp32:esp32:esp32s3:CDCOnBoot=cdc"}')
    z.writestr("flash_args", "--flash_mode dio --flash_freq 80m --flash_size 16MB\n0x0 PowerMeterRBAmp.ino.bootloader.bin\n"
                             "0x8000 PowerMeterRBAmp.ino.partitions.bin\n0xe000 boot_app0.bin\n0x10000 PowerMeterRBAmp.ino.bin\n")
