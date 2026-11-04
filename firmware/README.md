# PowerMeter firmware (ESP32-S3)

Firmware for the PowerManager meter: ESP32-S3 Super Mini + PZEM-004T + solid-state relay + WS2812 LED + tamper switch. Design: [`../docs/firmware.md`](../docs/firmware.md), protocol: [`../docs/protocol.md`](../docs/protocol.md).

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

Open `firmware/PowerMeter/PowerMeter.ino`, check the pins in `config.h`, then Upload.

If you also have other ESP32 board packages installed (e.g. **esp32-bluepad32** for game controllers), the Tools → Board menu has an "ESP32S3 Dev Module" in each of them. Pick the one under **esp32**. Bluepad32's package is based on the older 2.0.x core. The sketch builds on it too, but it is meant for Bluetooth controllers, not tested here, and stays behind on fixes.

## 3. Wiring (default pins, see `config.h`)

| Function | GPIO |
|----------|------|
| PZEM TX → ESP RX | 5 |
| PZEM RX ← ESP TX | 6 |
| SSR input (+) | 7 |
| Tamper switch (NC, other side to GND) | 4 |
| WS2812 data | 48 (onboard LED on most Super Mini boards) |
| DS3231 SDA / SCL (optional, set `HAS_DS3231 1`) | 8 / 9 |
| Battery sense (optional, set `BATTERY_ENABLE 1`) | 1, through a divider (see `config.h`) |

`HW_INFO` in `config.h` is free text (default `"PZEM-004T 1Chn"`) shown by **Get meter info** on the server.

Battery: wire `battery+ — R1 — ADC pin — R2 — GND` and set `BATTERY_DIVIDER = (R1+R2)/R2` plus the empty/full voltages (defaults: 1S Li-ion 3.30–4.20 V, 100 k + 100 k → 2.0). The percentage is a linear estimate.

Power the PZEM's TTL side from **3.3 V** (ESP32 pins are not 5 V tolerant). The SSR is not a safety device: keep a proper breaker.

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
