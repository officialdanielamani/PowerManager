# PowerMeter MC — multi-channel AC meter firmware (ESP32-S3)

A separate firmware for the multi-channel AC meter: **ESP32-S3 Super Mini + one or more rbAmp BASIC
modules (SCT-013 clamps, I²C) + DS3231 + tamper limit switch + I²C relay board(s)**, optionally DS18B20
temperature sensors. Everything else (Wi-Fi setup,
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
| DS18B20 data (optional, `TEMP_ENABLE 1`) | 6, 4.7 kΩ pull-up to 3.3 V (several sensors on the same wire) |

- **rbAmp:** VCC = **5 V** (4.5–5.5 V; it is isolated from the mains), GND, SDA, SCL, DRDY. It has
  4.7 kΩ pull-ups on SDA/SCL built in; with the DS3231 module (also pull-ups) on the same bus that is fine
  for short wires. One clamp per channel, arrow toward the load. The voltage input goes to L/N with a fuse
  (see the rbAmp hardware page).
- **Several rbAmp modules** share the same I²C bus; each needs its own address (see §3). With 4 or more
  modules, or a long bus, cut the on-board pull-ups on all but one module. DRDY comes from board 1.
- **Relay boards:** I²C address set with their A0–A2 jumpers (default here `0x20`). Which relay switches
  which channel is set in `CHANNELS`.
- Mains wiring must be done by a qualified person with the circuit switched off.

## 3. config.h

Everything about the hardware is in a few tables at the top:

```c
#define RBAMP_BOARDS { 0x50, 0x51 }                       // board 1, board 2 (I2C addresses)
#define RELAY_BOARDS { {RELAY_XL9535, 0x20, 8, 1} }       // driver, address, relays, active high
#define CHANNELS { \
  /* board  input  clamp     relay */ \
  {  1,     1,     SCT_100A, 1 },   /* CH1 */ \
  {  1,     2,     SCT_30A,  2 },   /* CH2 */ \
  {  2,     1,     SCT_20A,  3 },   /* CH3: second module, its input 1 */ \
}
```

| Setting | Meaning |
|---|---|
| `RBAMP_BOARDS` | I²C address of every rbAmp module (1 to 8). Board 1 = first entry |
| `RELAY_BOARDS` | every relay board: `RELAY_XL9535` (up to 16 relays) or `RELAY_PCF8574` (up to 8), address, number of relays, active high (XL9535: 1; PCF8574 with low-side SSRs: usually 0). Relays are numbered across the boards |
| `CHANNELS` | one line per channel, in order CH1, CH2 … (up to 16): rbAmp board, its current input (1–7), clamp model, relay (0 = none) |
| clamp models | `SCT_5A`, `SCT_10A`, `SCT_20A`, `SCT_30A`, `SCT_50A`, `SCT_60A`, `SCT_100A`, or `SCT_KEEP` (CT-series modules with factory-paired clamps) |
| AUX relays | relays no channel uses: manual ON / OFF from the server, not metered |
| `HAS_DS3231`, `TAMPER_ENABLE`, `TEMP_ENABLE`, `BATTERY_ENABLE`, `MESH_ENABLE` | optional parts; 0 = left out of the firmware completely (smaller binary) |
| `PIN_TEMP`, `TEMP_MAX_SENSORS`, `TEMP_READ_MS` | DS18B20 pin, how many sensors (T1, T2 … sorted by serial number), read interval |

Mistakes in the tables (a board number that does not exist, the same input or relay used twice) stop the
**compile** with a clear message. At start-up the meter checks the tables against the modules it finds (e.g.
"CH5: board 1 is an UI2 with 2 input(s): it has no input 5") and prints the channel map:
`[rbamp] CH1 = B1 input 1, relay 1: clamp in use SCT-013 100 A (code 5)`.

**Giving a module its own address** (every new rbAmp answers at `0x50`): connect **only that module**, open
the Serial Monitor (115200) and type

```
i2c scan
rbamp addr 0x50 0x51
```

The module stores the new address and restarts. Then put it in `RBAMP_BOARDS`. Do this once per extra module.
Type `help` for the list of serial commands.

## 4. How it works

- **Energy:** the rbAmp BASIC keeps no kWh. Every second the meter closes a measurement period on the module,
  reads each channel's average power and adds it to its own per-channel kWh counter (saved every 5 min and on
  every session change).
- **Limits:** max current per channel; kWh and time limits for the whole meter (**Shared** mode, default)
  or per channel (**Independent** mode) — chosen on the server. Without DRDY the module is read once a second;
  with DRDY on every 100 ms window, so an over-current trip happens within ~0.2 s.
- **Several modules:** each 100 ms window, every module's real-time block is read in one burst (inputs 4–7
  through the channel window). At 50 kHz two modules with 7 inputs in use still fit in one window.
- **Temperature (optional):** DS18B20 sensors are converted every `TEMP_READ_MS` and read one per control
  loop pass (no long pause). Readings go to the server with the next telemetry record (`"t": [24.5, 31.0]`).
  **Temperature Protection** on the server (max °C + run / relay off / end session) works like the voltage
  protection; it clears 5 °C below the limit.
- **Relays:** written on every change and again every 2 s (recovers from a glitch on the relay board). If the
  board stops answering the meter reports `relay_error` (server notification under *Sensor error*).
- **No AC:** the module has its own 5 V supply and keeps answering in a power cut (U ≈ 0 V), so "No AC" and
  "sensor fault" are reported separately.

## 5. Shared code

`../tools/check_shared.py` lists files that should be identical in both sketches (outbox, mesh, system, …).
Run it after changing one of them in either sketch.
