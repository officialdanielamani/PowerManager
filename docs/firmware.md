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
      The meter tests the network while the setup AP stays on, then shows its new IP
      (and http://power-xxxxxxxx.local). No restart needed; the setup AP closes 5 min later.
Normal use ──▶ STA mode, local web UI at http://<ip>/ (mDNS: power-xxxxxxxx.local)
   3. Login (local admin)
   4. Pair, using one of:
        - [Take photo of QR]: file input -> browser's built-in BarcodeDetector (Chrome/Edge on
          Android, Chrome desktop). Not every browser has it (e.g. iPhone Safari).
        - paste the "pairing text" shown under the QR on the server page
        - type address, port, secure, pairing code
   5. [Pair] -> POST /api/v1/pair -> success -> local dashboard
```

The page is one embedded HTML file (`src/webui.h`): no CDN, no extra files, no flash upload step, works fully offline.

- The browser's time is sent with every request to the local UI (`?t=<epoch>`) and used if the device has no better time.
- If Wi-Fi does not connect for **3 min** after boot, the AP opens **as well** (AP+STA) so the user can fix the settings. Once STA connects, the AP closes after 5 min.
- Factory reset: long-press BOOT 10 s (LED flashes white) → wipes NVS + LittleFS buffer. Also available on the local UI.

## 4. Local dashboard

Date/time · Device ID / name / info · V, A, W, PF, Hz · session kWh used / max · next reset · auto renew · server connection status · last sync.

- **Relay toggle**: ON is disabled in the UI **and refused in the handler** when quota is exceeded, the session expired, or the over-current trip is latched.
- [Send Data Now] · [Reset Trip] (when tripped) · Wi-Fi settings · Pairing status (re-pair only shown when not paired) · Change local admin password · [Factory Reset] · **Check OTA Update** (greyed out; future).
- The meter never knows which server users can see it: user links exist only on the server.
- Removing the device is done from the **server** ([Remove Device]); the meter then factory-resets itself (see protocol §2 `remove` / `4403`).

## 5. Tasks (FreeRTOS)

No busy-waiting: tasks sleep with `vTaskDelay`. The `control` task never waits on the network; `net` and `web` have a soft watchdog (see §11 Watchdogs).

| Task | Period | Core | Job |
|------|--------|------|-----|
| `control` | 250 ms (PZEM every 1 s) | 1 | Read PZEM (timeout 250 ms), tamper, run the rules, drive the SSR, queue telemetry. **Never waits on the network.** |
| `net` | event loop | 0 | Wi-Fi state machine, WS client, pairing, send buffered items, apply config/commands |
| `web` | event loop | 0 | Local HTTP server + DNS captive portal |
| `led` | 20 ms | 1 | Show the LED pattern for the highest-priority state |

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

- Stored on LittleFS as **append-only segment files** (`/ob/<n>.log`, 50 items each, one JSON line per item) plus the highest acknowledged `seq` in NVS. LittleFS is append-friendly, so writing an item is cheap; a segment is deleted once all its items are acknowledged.
- Keeps the newest **~500 items** (10 segments ≈ 8 h at 60 s); when full, the oldest segment is dropped. Configurable in `config.h` (minimum 30).
- Items are removed only after the server `ack` (protocol §2.4). The buffer survives reboot.
- Telemetry **and events** go through this same buffer in `seq` order — a live event never jumps the queue (protocol §2.4 ordering rule).
- The `net` task sends a `ping` message every 30 s and treats a missing `pong` (10 s) as a lost connection.

## 9. LED

Only the highest-priority active state is shown. One-shot patterns (marked ⚡) play once over the current state, then it comes back.

| Priority | State | Pattern |
|----------|-------|---------|
| 1 | Factory reset (BOOT held 10 s / removed by server) | White fast flash (10 Hz) |
| 2 | Boot / self-test | Short R → G → B sweep (once at power-on) |
| 3 | Over-current tripped (latched) | **Red solid** (relay OFF) |
| 4 | Quota exceeded / session expired | **Yellow blink** 1 Hz until renewed |
| 5 | Panel open (tamper) | Purple blink 1 Hz |
| 6 | AC lost (no voltage) **or PZEM not answering** (the two look the same: the PZEM is powered from AC) | Orange short flash every 3 s |
| 7 | Setup / AP mode (first boot or Wi-Fi fallback) | Blue slow breathe |
| 8 | Wi-Fi connecting | Blue blink 2 Hz |
| 9 | Not paired (Wi-Fi OK, no server credentials / `4401`) | Cyan slow breathe |
| 10 | Offline / server not replying | **Red blink** 1 Hz until online |
| ⚡ | Data sent and `ack` received | **Green blink ×2** |
| ⚡ | Config applied | Green long blink ×1 (500 ms) |
| ⚡ | Command rejected (e.g. relay ON while quota exceeded) | Red blink ×3 fast |
| ⚡ | Pairing success | Green blink ×5 fast |
| – | Normal idle, relay ON | Very dim green solid |
| – | Normal idle, relay OFF by user | Off |

Notes:
- Tamper and AC lost sit **below** trip/quota on purpose: the relay-blocking states matter more locally. Both are still reported to the server.
- LED brightness is set in `config.h` (default low, so it does not light up a room).

## 10. Libraries

Only two libraries from the Arduino Library Manager:

| Library | Use |
|---------|-----|
| ArduinoJson 7 (Benoit Blanchon) | JSON |
| WebSockets 2.6+ (Markus Sattler / Links2004) | WebSocket client with TLS + extra headers |

Everything else is built into the ESP32 core: WiFi, WebServer, DNSServer, ESPmDNS, HTTPClient, NetworkClientSecure, Preferences (NVS), LittleFS, `rgbLedWrite()` for the WS2812, FreeRTOS. The PZEM Modbus protocol and the optional DS3231 (raw I2C via `Wire`) are implemented in the sketch — no extra libraries.

**Arduino IDE board settings:** Board *ESP32S3 Dev Module* · USB CDC On Boot: *Enabled* · Flash Size: *4MB* · Partition Scheme: **Minimal SPIFFS (1.9MB APP with OTA/128KB SPIFFS)** (the 128 KB "SPIFFS" partition holds the LittleFS offline buffer; the app uses ~66% of its slot).

Tested to compile with Arduino-ESP32 core 3.3.12, ArduinoJson 7.4.2, WebSockets 2.7.3.

## 11. Sketch layout

```
firmware/PowerMeter/
  PowerMeter.ino      setup(): init + start tasks; loop(): BOOT button + soft watchdog
  config.h            pins, limits, timings, defaults
  src/
    app_state.*       shared state + mutex
    storage.*         NVS: identity, admin, Wi-Fi, server, config, session, seq counter
    outbox.*          offline buffer (LittleFS segments)
    sensor_pzem.*     Sensor interface + PZEM-004T Modbus driver
    control.*         rules, sessions, relay, events, telemetry
    net.*             Wi-Fi, pairing, WebSocket protocol
    web.* webui.h     local web server + embedded page
    led.*             status LED patterns
    timekeep.*        clock sources (server / NTP / DS3231 / browser)
    system.*          factory reset, soft watchdog
    certs.h           root CAs for TLS (Let's Encrypt, Google Trust Services, SSL.com)
```

The sensor sits behind a small `Sensor` interface (`begin()`, `channels()`, `read(ch, Reading&)`), so other sensor types and multi-channel boards can be added later.

### Watchdogs

- The `control` task never touches the network, so a stuck TLS handshake or HTTP call cannot stop limit enforcement.
- `net` and `web` report a heartbeat; `loop()` restarts the meter if one is stuck (net: 3 min, web: 1 min). Relay state, trip latch and session survive the restart (NVS).
