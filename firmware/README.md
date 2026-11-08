# PowerMeter firmware (ESP32-S3)

Firmware for the PowerManager meter: ESP32-S3 Super Mini + power sensor + solid-state relay + WS2812 LED + tamper switch.

Two sketches (open the folder's `.ino` in Arduino IDE):

| Sketch | Meter |
|---|---|
| `PowerMeterPZEM/PowerMeterPZEM.ino` | single channel: PZEM-004T (AC) or PZEM-017 (DC) — this page |
| `PowerMeterRBAmp/PowerMeterRBAmp.ino` | multi-channel AC: one or more rbAmp modules — see [`PowerMeterRBAmp/README.md`](PowerMeterRBAmp/README.md) |

(Until 0.8 they were called `PowerMeter` and `PowerMeterMC`. The firmware type stored on the meter is unchanged, so
flashing the renamed sketch keeps the meter's data, pairing and history.) Example configurations: [`examples/`](examples/).

**AC or DC meter:** one sketch, two firmwares. In `config.h` set `METER_DC 0` for the **AC meter** (PZEM-004T) or `METER_DC 1` for the **DC meter** (PZEM-017). Everything else (limits, sessions, mesh, offline buffer, local page) is the same. The meter tells the server its type when it pairs, so the server shows it as AC or DC automatically and uses the matching price. Design: [`../docs/firmware.md`](../docs/firmware.md), protocol: [`../docs/protocol.md`](../docs/protocol.md).

## 1. Arduino IDE setup (once)

1. Install **Arduino IDE 2.x**.
2. *File → Preferences → Additional boards manager URLs*, add:
   `https://espressif.github.io/arduino-esp32/package_esp32_index.json`
3. *Tools → Board → Boards Manager*: install **esp32 by Espressif Systems**. Reference version: **3.3.5** (the one used on the real meters). Also compiles on 3.3.12 and 2.0.17.
4. *Tools → Manage Libraries*: install
   - **ArduinoJson** by Benoit Blanchon (7.x)
   - **WebSockets** by Markus Sattler (2.6 or newer)

   No other libraries are needed. The LED, PZEM and DS3231 code is built in.

## 2. Board settings

| Setting | Value |
|---------|-------|
| Board | ESP32S3 Dev Module, from the **esp32** (Espressif) list |
| USB CDC On Boot | Enabled (serial log over the USB-C port) |
| Flash Size | 4MB |
| Partition Scheme | **Minimal SPIFFS (1.9MB APP with OTA/128KB SPIFFS)** |

Open `firmware/PowerMeterPZEM/PowerMeterPZEM.ino`, check the pins in `config.h`, then Upload.

If you also have other ESP32 board packages installed (e.g. **esp32-bluepad32** for game controllers), the Tools → Board menu has an "ESP32S3 Dev Module" in each of them. Pick the one under **esp32**. Bluepad32's package is based on the older 2.0.x core. The sketch builds on it too, but it is meant for Bluetooth controllers, not tested here, and stays behind on fixes.

## 3. Wiring (default pins, see `config.h`)

| Function | GPIO |
|----------|------|
| PZEM TX → ESP RX (DC: RS485 module RO/RXD) | 5 |
| PZEM RX ← ESP TX (DC: RS485 module DI/TXD) | 6 |
| DC only: MAX485 DE+RE (set `PIN_RS485_DE`; `-1` for auto-direction modules) | – |
| SSR input (+) (`RELAY_GPIO_PINS`) | 7 |
| Tamper switch (NC, other side to GND) | 4 |
| WS2812 data | 48 (onboard LED on most Super Mini boards) |
| I²C SDA / SCL (optional: DS3231, PCF8574 / XL9535 relay boards, SHT sensors) | 8 / 9 |
| Battery sense (optional, set `BATTERY_ENABLE 1`) | 1, through a divider (see `config.h`) |
| DS18B20 temperature sensor(s) (optional, set `TEMP_DS18B20 1`) | 10, 4.7 kΩ pull-up to 3.3 V |

**Relay outputs** (both sketches, `RELAY_BOARDS` in `config.h`): ESP32 pins (`RELAY_GPIO` + `RELAY_GPIO_PINS`),
PCF8574 (up to 8 outputs) and XL9535 (up to 16) boards, mixed, e.g. `{ {RELAY_GPIO, 0, 1, 1}, {RELAY_PCF8574, 0x20, 6, 0} }`
= an SSR on GPIO 7 plus 6 outputs of a PCF8574, or PCF8574 (0-4) + PCF8574 (0-6) = `{ {RELAY_PCF8574, 0x20, 5, 0},
{RELAY_PCF8574, 0x21, 7, 0} }`. Relays are numbered across the entries. On the PZEM meter `CHANNEL_RELAY` (default 1)
switches the measured line; every other relay is an **AUX relay** with manual / schedule / event control from the
server (see `docs/multichannel.md` § AUX relays). Mistakes (too many outputs on a board, a GPIO relay without a pin)
stop the compile with a message.

**Temperature / humidity:** DS18B20 (`TEMP_DS18B20`) and SHT20 / SHT3x / SHT4x on the I²C bus (`TEMP_SHT`,
`SHT_SENSORS { {SHT_4X, 0x44} }`) can be used together; T1, T2 … = the SHTs in config order, then the DS18B20s.
SHTs add humidity (%RH), shown next to the temperature and usable in AUX event rules.

**Optional parts** (`config.h`, 0 = not fitted): `HAS_DS3231`, `TAMPER_ENABLE`, `TEMP_DS18B20`, `TEMP_SHT`, `BATTERY_ENABLE`,
`MESH_ENABLE`. A part switched off is left out of the firmware completely, so the binary gets smaller; the server
hides what the meter does not have (e.g. the tamper settings with `TAMPER_ENABLE 0`).

What each switch costs (multi-channel firmware 0.8.0, ESP32-S3, measured):

| Part | Flash |
|---|---|
| Mesh (`MESH_ENABLE`) | ~18 KB |
| Battery sense (`BATTERY_ENABLE`, ADC calibration) | ~11 KB |
| Temperature (`TEMP_DS18B20`) | ~5 KB |
| DS3231 (`HAS_DS3231`) | ~1 KB |
| Tamper switch (`TAMPER_ENABLE`) | < 1 KB |

Default build ≈ 1.39 MB, everything optional off ≈ 1.37 MB. Most of the firmware is Wi-Fi, TLS, the web server
and JSON, which every meter needs, so the switches save little: the *Minimal SPIFFS* partition scheme (1.9 MB
app) is still required (the default scheme's 1.2 MB app space is too small either way).

**Temperature (DS18B20):** up to `TEMP_MAX_SENSORS` (4) on one wire, numbered T1, T2 … by serial number (stable
over a restart). Read every `TEMP_READ_MS` (10 s), sent with the readings, shown on the server (Info page, dashboard,
history chart, Telegram) with an optional **Temperature Protection** (max °C, then run / relay off / end session).
No extra Arduino library is needed.

`HW_INFO` in `config.h` is free text (default `"AC Power Sensor 1Chn"` / `"DC Power Sensor 1Chn"`) shown by **Get meter info** on the server.

**DC meter (PZEM-017):** it talks RS485 (9600 baud, 8N2), so put an RS485-to-TTL module (3.3 V) between it and the ESP32: module A/B to PZEM A/B, module RO→GPIO5, DI←GPIO6. Modules with automatic direction need nothing else; a plain MAX485 board needs DE and RE tied together to a free GPIO set as `PIN_RS485_DE`. Set the shunt you use in `PZEM017_SHUNT_A` (50 / 100 / 200 / 300 A; the meter writes it to the PZEM at start-up; `0` = leave it). The PZEM-017 is powered from the measured DC line (7–300 V), so like the AC version it does not answer when the DC supply is off.

Battery: wire `battery+ — R1 — ADC pin — R2 — GND` and set `BATTERY_DIVIDER = (R1+R2)/R2` plus the empty/full voltages (defaults: 1S Li-ion 3.30–4.20 V, 100 k + 100 k → 2.0). The percentage is a linear estimate.

Power the PZEM's TTL side from **3.3 V** (ESP32 pins are not 5 V tolerant). The SSR is not a safety device: keep a proper breaker.

**SSR drive:** a GPIO gives 3.3 V, and many SSRs (e.g. "SSR-25DA", input 3–32 V DC) are at their limit there; when Wi-Fi transmits, the 3.3 V rail dips and the SSR can miss AC half-cycles (a lamp then flickers / glows brighter and dimmer). Best: drive the SSR from **5 V through a small NPN transistor or logic MOSFET** (e.g. 2N2222 / BC547 with 1 k base resistor, or AO3400), and put a 100–470 µF capacitor on the ESP's supply. Use a **zero-cross ("DA") SSR** for AC loads. The firmware already uses the strongest pin drive and waits at least 2 s before switching ON again after an OFF (`RELAY_MIN_OFF_MS`), and prints `[relay] ON/OFF - reason` on serial for every change.

## 4. First start

Open the Serial Monitor (115200). It shows the device ID and the **setup Wi-Fi name + password**:

```
 PowerMeter 0.1.0   Device ID: K7P29QXM
 Setup Wi-Fi: Power_K7P29QXM   password: K7P29QXM   (ON now, open http://192.168.4.1)
```

- By default the password is the **same code as in the Wi-Fi name** (here `K7P29QXM`), so you can set it up without the serial monitor. It's easy to guess, which is acceptable because the hotspot is only for setup. Create the admin account soon after powering a new meter. For a random password instead, set `AP_PASSWORD_IS_DEVICE_ID 0` in `config.h`.
- Missed the message? It repeats every 30 s while the setup hotspot is on, or type `info` + Enter in the Serial Monitor.

1. Connect your phone to `Power_XXXXXXXX`. The setup page opens by itself, or browse to `http://192.168.4.1`.
2. Create the meter's admin account.
3. Pick your Wi-Fi. The page shows the meter's new address, e.g. `http://192.168.1.57`.
4. Switch the phone back to your Wi-Fi, open that address and log in.
5. On the server: **Add New Meter**. On the meter page either take a photo of the QR, paste the pairing text, or type the details, then press **Pair**.

**Factory reset:** hold BOOT for 10 s (LED flashes white), or use *Settings → Factory reset*.

## 5. LED

| LED | Meaning |
|-----|---------|
| Green blink ×2 | Data sent and confirmed by server |
| Red blinking | Server offline / not reachable |
| Red solid | Over-current trip (relay off until Reset Trip) |
| Yellow blinking | kWh quota used up or session ended (until renewed) |
| Purple blinking | Panel open (tamper) |
| Orange flash every 3 s | No AC voltage (or PZEM not answering) |
| Blue breathing / blinking | Setup mode / connecting to Wi-Fi |
| Cyan breathing | Wi-Fi OK, not paired yet |
| Dim green / off | Normal, relay on / off |
| Very dim red | Disabled by the server |

The full table is in `docs/firmware.md` §9.

## 6. Mesh (no Wi-Fi at the meter)

Meters can reach the server through a nearby meter that is on Wi-Fi (ESP-NOW). Same firmware; default role **Auto**
(Wi-Fi first, mesh after 90 s without Wi-Fi, online meters relay for others). Settings → Mesh on the meter's page, or
*Use mesh only* during setup. Details and a test plan: [`../docs/mesh.md`](../docs/mesh.md).
