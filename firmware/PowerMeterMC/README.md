# PowerMeter MC — multi-channel AC meter firmware (ESP32-S3)

A separate firmware for the multi-channel AC meter: **ESP32-S3 Super Mini + rbAmp BASIC module
(SCT-013 clamps, I²C) + DS3231 + tamper limit switch + I²C relay board**. Everything else (Wi-Fi setup,
pairing, the meter's own page, mesh, offline buffer, safe mode) works like the single-channel firmware
in `../PowerMeter`. Design and protocol: [`../../docs/multichannel.md`](../../docs/multichannel.md).

## 1. Arduino IDE

Same as `../PowerMeter` (board package **esp32 by Espressif Systems 3.3.5**, ArduinoJson, WebSockets
by Markus Sattler; **ESP32S3 Dev Module**, USB CDC On Boot Enabled, Flash 4 MB, Partition Scheme
*Minimal SPIFFS*). Open `PowerMeterMC/PowerMeterMC.ino`.

## 2. Wiring (default pins, see `config.h`)

| Function | GPIO / bus |
|----------|------------|
| I²C SDA / SCL (rbAmp, DS3231, relay board) | 8 / 9, **50 kHz** |
| rbAmp DRDY | 5 (internal pull-up; add 10 kΩ to 3.3 V on long wires) |
| Tamper switch (NC, other side to GND) | 4 |
| WS2812 data | 48 |
| Battery sense (optional, `BATTERY_ENABLE 1`) | 1, through a divider |

- **rbAmp:** VCC = **5 V** (4.5–5.5 V; it is isolated from the mains), GND, SDA, SCL, DRDY. It has
  4.7 kΩ pull-ups on SDA/SCL built in; with the DS3231 module (also pull-ups) on the same bus that is fine
  for short wires. One clamp per channel, arrow toward the load. The voltage input goes to L/N with a fuse
  (see the rbAmp hardware page).
- **Relay board:** I²C address set with its A0–A2 jumpers (default here `0x20`). Relay 1 = channel 1, …
- Mains wiring must be done by a qualified person with the circuit switched off.

## 3. config.h

| Setting | Meaning |
|---|---|
| `RBAMP_CT_MODELS` | SCT-013 model per channel: 1 = 5 A, 2 = 10 A, 6 = 20 A, 3 = 30 A, 4 = 50 A, 7 = 60 A, 5 = 100 A, 0 = leave (CT-series modules) |
| `RELAY_DRIVER` | `RELAY_XL9535` (XL9535 relay board) or `RELAY_PCF8574` (PCF8574 + SSRs / relay module) |
| `RELAY_ADDRESS`, `RELAY_COUNT` | board address and number of relays (1/2/4/8) |
| `RELAY_ACTIVE_HIGH` | XL9535 boards: 1. PCF8574 driving low-side SSR / relay inputs: usually 0 |
| `HAS_DS3231` | 1 (used in this project) |
| `PIN_RDY` | the DRDY pin (read on every 100 ms window when **Read data when RDY** is ticked on the server) |

The module variant (UI1/UI2/UI3/UI5/UI7 or I1/I2/I3) is read from the module: channel count and whether it
has a voltage input are automatic. The serial banner shows what was found, e.g.
`Sensor: rbAmp UI2, 2 channel(s)   Relays: XL9535 at 0x20 OK   Mode: shared`.

## 4. How it works

- **Energy:** the rbAmp BASIC keeps no kWh. Every second the meter closes a measurement period on the module,
  reads each channel's average power and adds it to its own per-channel kWh counter (saved every 5 min and on
  every session change).
- **Limits:** max current per channel; kWh and time limits for the whole meter (**Shared** mode, default)
  or per channel (**Independent** mode) — chosen on the server. Without RDY the module is read once a second;
  with RDY on every 100 ms window, so an over-current trip happens within ~0.2 s.
- **Relays:** written on every change and again every 2 s (recovers from a glitch on the relay board). If the
  board stops answering the meter reports `relay_error` (server notification under *Sensor error*).
- **No AC:** the module has its own 5 V supply and keeps answering in a power cut (U ≈ 0 V), so "No AC" and
  "sensor fault" are reported separately.

## 5. Shared code

`../tools/check_shared.py` lists files that should be identical in both sketches (outbox, mesh, system, …).
Run it after changing one of them in either sketch.
