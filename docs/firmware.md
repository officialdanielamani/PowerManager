# Meter Firmware (ESP32-S3 Super Mini)

Built with **Arduino IDE 2.x** and the **Arduino-ESP32 core 3.x**. The folder is laid out so it can move to PlatformIO later without code changes.

## 1. Hardware

| Part | Notes |
|------|-------|
| ESP32-S3 Super Mini | 4 MB flash. Powered from a **DC UPS**, so it stays up during an AC outage. |
| PZEM-004T (v3/v4 protocol, Modbus-RTU over TTL) | Its measurement side is powered from **AC**: when AC is lost it stops answering → this is how "No Voltage" is detected. Energy register keeps its value without power. Power the TTL side from **3.3 V** (or use a divider on its TX) — ESP32 pins are not 5 V tolerant. |
| Solid-state relay | Rated well above the load, with a heatsink. **Not a protection device** — a breaker is still required. SSRs can fail shorted. |
| Tamper switch | Normally-closed to GND. Open circuit = panel open (a cut wire also shows as open). |
| WS2812 RGB LED | Onboard (GPIO48) or external |
| DS3231 RTC (optional) | Keeps time with no network after a reboot |

### Proposed pin map (check against your board's silkscreen)

| Function | GPIO | Notes |
|----------|------|-------|
| PZEM RX (← PZEM TX) | 5 | UART1 |
| PZEM TX (→ PZEM RX) | 6 | UART1 |
| SSR control | 7 | Active-high, with a default-OFF pull-down |
| Tamper input | 4 | `INPUT_PULLUP` |
| I2C SDA (DS3231) | 8 | optional |
| I2C SCL (DS3231) | 9 | optional |
| RGB LED | 48 | onboard WS2812 |
| BOOT button | 0 | Long press 10 s = factory reset |

Avoid: GPIO 0/3/45/46 (strapping), 19/20 (USB), 43/44 (UART0 / serial log).

**Pins are not a problem:** this uses 6 pins plus the onboard LED, and the Super Mini exposes about 15 free GPIOs, so the **DS3231 fits easily**. For future multi-channel meters, **several PZEMs can share the same UART** using different Modbus addresses, so more channels need no extra pins (only extra SSR pins).

## 2. Device identity

- `device_id` = 8 characters (base32 of a hash of the eFuse MAC), e.g. `MTR23H4R`. Public, used in the SSID and on the server.
- SSID: `Power_<device_id>`.
- AP password: **8 random characters made on first boot**, stored in NVS, printed on the serial log at every boot. Write it on a label. (It is not derived from the MAC: the MAC is visible in the Wi-Fi BSSID and the firmware is public, so a derived password could be computed by anyone.)

## 3. Setup flow

```
First boot ──▶ AP mode (Power_XXXXXXXX), captive portal at 192.168.4.1
   1. Create local admin (username + password, same rules as server)
   2. Wi-Fi: scan list or manual SSID + password
   3. Save → restart
Boot with Wi-Fi ──▶ STA mode, local web UI at http://<ip>/ (mDNS: power-xxxxxxxx.local)
   4. Login (local admin)
   5. Pair: [Take photo of QR] (file input → decoded with jsQR in the browser)
            or manual: address, port, secure, pairing code
   6. [Pair] → POST /api/v1/pair → success → local dashboard
```

- The browser's time is sent with every request to the local UI (`?t=<epoch>`) and used if the device has no better time.
- If Wi-Fi does not connect for **3 min** after boot, the AP opens **as well** (AP+STA) so the user can fix the settings. Once STA connects, the AP closes after 5 min.
- Factory reset: long-press BOOT 10 s (LED flashes white) → wipes NVS + LittleFS buffer. Also available on the local UI.

## 4. Local dashboard

Date/time · Device ID / name / info · V, A, W, PF, Hz · session kWh used / max · next reset · auto renew · server connection status · last sync.

- **Relay toggle**: ON is disabled in the UI **and refused in the handler** when quota is exceeded, the session expired, or the over-current trip is latched.
- [Send Data Now] · [Reset Trip] (when tripped) · Wi-Fi settings · Pairing (re-pair / unpair) · Change local admin password.

## 5. Tasks (FreeRTOS)

No `delay()`-based blocking anywhere. Every task feeds the task watchdog (timeout 10 s → reboot).

| Task | Period | Core | Job |
|------|--------|------|-----|
| `control` | 1 s | 1 | Read PZEM (timeout 200 ms), tamper, run the rules, drive the SSR. **Never waits on the network.** |
| `net` | event loop | 0 | Wi-Fi state machine, WS client, pairing, send buffered items, apply config/commands |
| `web` | event loop | 0 | Local HTTP server + DNS captive portal |
| `led` | 50 ms | 0 | Show the LED pattern for the highest-priority state |
| `store` | on demand | 0 | Writes to the LittleFS ring buffer and NVS (so flash writes never block `control`) |

State is shared through one struct protected by a mutex. Tasks talk to each other through FreeRTOS queues.

## 6. Rules (in `control`)

```
relay_on = want_on
           AND NOT tripped
           AND session.status == active
           AND NOT (offline_off_min > 0 AND offline_for ≥ offline_off_min)
```

- **Over-current**: A > `amp_max` for 2 reads in a row → `tripped = true` (stored in NVS, survives reboot), relay OFF, event `overcurrent`.
- **Quota**: `session_kwh = e_now − e_at_session_start ≥ kwh_max` → status `quota`, relay OFF, event.
- **Session expiry**: `now ≥ next_reset` (needs valid time) → end session `expired`; if `auto` → start a new one at once; else relay OFF until `renew`.
- **No voltage**: PZEM no reply ×3 or V < 50 V → `ac_lost` event (relay state kept; there is no load power anyway).
- **Tamper**: input change (debounced 200 ms) → event.
- Session data (`sid`, `e_at_start`, `start`, `status`) is stored in NVS **only when it changes**, so there is no flash wear.
- PZEM energy register reset / swap is detected (`e_now < e_at_start`) → the start value is re-based and a warning is logged.

## 7. Time

Priority: **server time** (`welcome`, `ack`, `time`) > NTP (if internet) > DS3231 (if fitted) > browser time from the local UI.
With no valid time: telemetry uses `tv:0` + uptime (protocol §2.4), and **daily reset is paused** until time is valid (duration mode keeps counting by uptime).

## 8. Offline buffer

- Ring buffer file on LittleFS, fixed-size records (~40 B). **Default 500 items** (≈ 8 h at 60 s); configurable, minimum 30.
- When full, the oldest items are overwritten (events are kept before telemetry when space runs out).
- Items are removed only after the server `ack` (protocol §2.4). The buffer survives reboot.

## 9. LED

Highest priority first:

| Priority | State | Pattern |
|----------|-------|---------|
| 1 | Factory reset in progress | White fast flash |
| 2 | Over-current tripped | **Red solid** (relay OFF) |
| 3 | Quota exceeded / session expired | **Yellow blink** 1 Hz until renewed |
| 4 | Setup / AP mode | Blue slow breathe |
| 5 | Offline / server not replying | **Red blink** 1 Hz until online |
| 6 | Data sent and `ack` received | **Green blink ×2**, then off |
| – | Normal idle | Off (or very dim green, optional) |

## 10. Libraries (Arduino Library Manager)

| Library | Use |
|---------|-----|
| ArduinoJson 7 | JSON |
| WebSockets (Markus Sattler / links2004) | WS client with TLS and extra headers |
| Adafruit NeoPixel | WS2812 |
| PZEM004Tv30 (mandulaj) | PZEM Modbus |
| RTClib (Adafruit) | DS3231, optional |
| Built in to the core | WiFi, WebServer, DNSServer, ESPmDNS, Preferences (NVS), LittleFS, HTTPClient, NetworkClientSecure |

Exact versions will be pinned in `firmware/README.md` when code starts.

**Arduino IDE board settings:** ESP32S3 Dev Module · USB CDC On Boot: Enabled · Flash 4 MB · Partition: custom `partitions.csv` in the sketch folder (app ×2 for future OTA + ~1 MB LittleFS).

## 11. Sketch layout

```
firmware/PowerMeter/
  PowerMeter.ino        setup()/loop() only, starts tasks
  config.h              pins, defaults, limits
  partitions.csv
  src/
    state.*  control.*  net.*  web.*  led.*  store.*  pairing.*  timekeep.*
    sensors/ sensor.h  pzem004t.*      ← interface so other sensors can be added
  data/                 local web UI (html/js/css incl. jsQR, gzipped) → LittleFS
```

The sensor sits behind a small `Sensor` interface (`begin()`, `channels()`, `read(ch, Reading&)`), so other sensor types and multi-channel boards can be added later.
