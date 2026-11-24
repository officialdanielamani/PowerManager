# rbAmp debug tool

`RBAmpScanner/RBAmpScanner.ino`: an Arduino sketch for the ESP32-S3 to check an rbAmp module without the meter firmware.
Open the Serial Monitor at 115200 baud (line ending *Newline*) and type `help`. Set the pins at the top of the sketch
(SDA 7, SCL 9, DRDY 4 by default) or with `pins <sda> <scl> <drdy>`.

| Command | What it does |
|---|---|
| `bus` | idle levels of SDA / SCL, a test address nobody has (flood check), bus recovery |
| `scan` | all addresses; when the bus ACKs everywhere (an rbAmp does this) only addresses that also return data are listed |
| `use 0x51` | the module the other commands talk to (default 0x50) |
| `id` | product ID, variant (UI1 … UI7, I1 … I3), voltage input, version, AC frequency, uptime, clamp model of every input |
| `data` | one reading: voltage, Hz, A / W / PF of every input |
| `watch 30` | a reading every second for 30 s (any character stops it) |
| `drdy` | counts the DRDY pulses for 2 s |
| `reg 0x54 [n]` / `wreg 0x05 0x03` | raw register read / write |
| `addr 0x50 0x51` | changes the I2C address (only ONE rbAmp on the bus), like the meter firmware does |
| `reset`, `speed 50000`, `pins …` | module reset, I2C clock, pins |

The register map is the one of `firmware/PowerMeterRBAmp/src/rbamp.cpp`. Not tested on hardware by the author.
