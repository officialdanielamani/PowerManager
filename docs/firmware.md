# Meter Firmware (ESP32-S3 N16R8)

Built with **Arduino IDE 2.x** and the **Arduino-ESP32 core 3.x**. One source builds the **AC meter** (`METER_DC 0`, PZEM-004T, TTL 9600 8N1, registers 0x0000–0x0009) or the **DC meter** (`METER_DC 1`, PZEM-017, RS485 9600 8N2 via an RS485-TTL module, registers 0x0000–0x0007: V 0.01 V, A 0.01 A, W 0.1 W, Wh; shunt range written to holding register 0x0003 at start-up). Both share one Modbus transport (`sensor_pzem.cpp`); the rest of the firmware is identical. **Readings:** recorded every `read_s` into the offline buffer and sent as a batch every `report_s`; events and *Get Data Now* trigger an immediate send. The folder is laid out so it can move to PlatformIO later without code changes.

## 1. Hardware

| Part | Notes |
|------|-------|
| ESP32-S3 N16R8 (16 MB flash, 8 MB octal PSRAM) | Powered from a **DC UPS**, so it stays up during an AC outage. GPIO 35–37 are used by the octal PSRAM and must not be wired. |
| PZEM-004T (v3/v4 protocol, Modbus-RTU over TTL) | Its measurement side is powered from **AC**: when AC is lost it stops answering → this is how "No Voltage" is detected. Energy register keeps its value without power. Power the TTL side from **3.3 V** (or use a divider on its TX) — ESP32 pins are not 5 V tolerant. |
| Solid-state relay | Rated well above the load, with a heatsink. **Not a protection device** — a breaker is still required. SSRs can fail shorted. |
| Tamper switch | Normally-closed to GND. Open circuit = panel open (a cut wire also shows as open). |
| WS2812 RGB LED | Onboard (GPIO48) or external |
| DS3231 RTC (optional) | Keeps time with no network after a reboot |
| Battery sense (optional) | Resistor divider from the UPS/backup battery to an ADC pin (`BATTERY_ENABLE`), reported by *Get meter info* and on the local page |

### Proposed pin map (check against your board's silkscreen)

| Function | GPIO | Notes |
|----------|------|-------|
| PZEM RX (← PZEM TX) | 5 | UART1 |
| PZEM TX (→ PZEM RX) | 6 | UART1 |
| SSR control | 7 | Active-high, with a default-OFF pull-down |
| Tamper input | 4 | `INPUT_PULLUP` |
| I2C SDA (DS3231) | 8 | optional |
| I2C SCL (DS3231) | 9 | optional |
| Battery ADC | 1 | optional, ADC1 (works with Wi-Fi on); via divider, keep below ~3.1 V |
| RGB LED | 48 | onboard WS2812 |
| BOOT button | 0 | Long press 10 s = factory reset |

Avoid: GPIO 0/3/45/46 (strapping), 19/20 (USB), 43/44 (UART0 / serial log).

**Pins are not a problem:** this uses 6 pins plus the onboard LED, and the board exposes about 15 free GPIOs, so the **DS3231 fits easily**. For future multi-channel meters, **several PZEMs can share the same UART** using different Modbus addresses, so more channels need no extra pins (only extra SSR pins).

## 2. Device identity

- `device_id` = 8 characters (base32 of a hash of the eFuse MAC), e.g. `MTR23H4R`. Public, used in the SSID and on the server.
- SSID: `Power_<device_id>`.
- AP password: by default **the device ID itself** (`AP_PASSWORD_IS_DEVICE_ID 1`), e.g. SSID `Power_K7P29QXM`, password `K7P29QXM`. Chosen for easy setup; anyone who sees the SSID can join, so the risk window is first setup (before the admin account exists). Later fallback hotspots still need the admin login. Set it to `0` for a random 8-character password made on first boot (stored in NVS, kept after factory reset). The banner with SSID/password prints at boot (after waiting up to 3 s for USB serial), every 30 s while the hotspot is on, and on the serial command `info`.

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
- **Quota**: `session_kwh = e_now − e_at_session_start ≥ kwh_max` → per `kwh_action`: **warn** (*Session run as usual*): `quota` once + `sess.over`, power stays on; **hold** (*Off relay, but session continue*, default): relay OFF, session ends at its time limit or on Renew; **end** (*End session*): `quota` + `session_end` (reason `quota`) now. `session_end` is sent exactly once per session (`sess.ended`).
- **Operating voltage**: outside `volt.min`–`volt.max` for 3 readings → `voltage_out`; back inside for 10 readings → `voltage_ok`. Action **run** = event only, **hold** = relay OFF while out of range, **end** = session ends (status `ended`, reason `voltage`). Below 50 V counts as "No AC", not as out of range.
- **Tare** (`tare.a`, `tare.w`): while the PZEM gives readings the meter adds `tare.w × time` to the session's own-consumption counter (saved every 10 min). With `tare.limits` the kWh limit compares *session kWh − own consumption* and the over-current check uses *A − tare.a*. Telemetry and `state` report both `kwh` (raw) and `kwh_t` (tared); raw readings are never changed.
- **Disabled** (server `disable`): session ends (reason `disabled`), relay OFF, Relay ON refused, no new sessions while disabled. `enable` does not start one either: the ended session keeps the relay off until **Renew**. Persisted in NVS.
- **Tamper protection** (if enabled): panel opens → **run** = event only, **hold** = relay OFF while open, **end** = session ends (reason `tamper`). Without protection, tamper is only reported.
- **Sensor timeout** (`sensor.timeout_s`, default 10 s, 0 = off): no valid PZEM reading for that long → `sensor_error` (`no_reply` = no answer, `bad` = garbled/CRC) with the configured action: **run** = event only, **hold** = relay OFF until a valid reading returns (`sensor_ok`), **end** = session ends (reason `sensor`). The timeout counts from boot. Note: without AC the PZEM does not answer either, so a power cut longer than the timeout also raises it.
- **Enable after Disable** does not start a session: the ended session keeps the relay OFF until **Renew**.
- **Session expiry**: `now ≥ next_reset` (needs valid time) → end session `expired` (unless a kWh limit already ended it); if `auto` → start a new one at once; else relay OFF until `renew`.
- Every telemetry item carries the session progress `s: {sid, kwh}`, so the server's "session used" stays current between `state` messages.
- The local dashboard shows **Time elapsed / Time left** for the session.
- **No voltage**: PZEM no reply ×3 or V < 50 V → `ac_lost` event (relay state kept; there is no load power anyway).
- **Tamper**: input change (debounced 200 ms) → event.
- Session data (`sid`, `e_at_start`, `start`, `status`) is stored in NVS **only when it changes**, so there is no flash wear.
- PZEM energy register reset / swap is detected (`e_now < e_at_start`) → the start value is re-based and a warning is logged.

## 7. Time

Priority: **server time** (`welcome`, `ack`, `time`) > NTP (if internet) > DS3231 (if fitted) > browser time from the local UI.
With no valid time: telemetry uses `tv:0` + uptime (protocol §2.4), and **daily reset is paused** until time is valid (duration mode keeps counting by uptime).
The DS3231 is only written from the net task (never inside the SNTP sync callback, which runs in the lwIP task with a very small stack).

At boot the serial monitor prints a `[boot] <step>` line before each init step (storage, offline buffer, clock, LED, sensor + relay, Wi-Fi + mesh, web server, tasks), so a crash shows which step it happened in.

### Crash report and safe mode

Each task writes a short "breadcrumb" (what it is doing now) into RTC memory, which survives a crash reboot. On the next boot the serial monitor prints the reset reason and, after a crash, a `[crash]` block: uptime at the crash, lowest free heap, the last boot step, and the last breadcrumb of `loop`, `net`, `web`, `control` and `led`. This works even when the ESP-IDF panic handler itself fails ("Re-entered core dump").

**Safe mode:** after 3 crashes in a row, each within 2 minutes of boot, the meter starts without the ESP-NOW mesh and without the DS3231, so it keeps measuring, enforcing limits and reporting. The banner shows `SAFE MODE`. A run that stays up for 2 minutes clears the count, so the next reboot is normal again.

"Get meter info" on the server shows **Last reset** (power on / restart / CRASH … / brownout, plus SAFE MODE).

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
| – | Disabled by the server | Very dim red, steady |
| 2 | Boot / self-test | Short R → G → B sweep (once at power-on) |
| 3 | Over-current tripped (latched) | **Red solid** (relay OFF) |
| 4 | Quota exceeded / session expired | **Yellow blink** 1 Hz until renewed |
| 5 | Panel open (tamper) | Purple blink 1 Hz |
| 6 | AC lost (no voltage), PZEM not answering, **or voltage out of the operating range** | Orange short flash every 3 s |
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

**Arduino IDE board settings:** Board *ESP32S3 Dev Module* · USB CDC On Boot: *Enabled* · Flash Size: *16MB* · PSRAM: *OPI PSRAM* · Partition Scheme: **Custom** (the `partitions.csv` in the sketch folder, see below). The 3.8 MB `spiffs` partition holds the LittleFS offline buffer.

**ESP32-S3 N16R8 (16 MB flash, 8 MB octal PSRAM), custom partition table** (`partitions.csv` in each sketch folder, identical in both, checked by `check_shared.py`):

| Partition | Offset | Size | Use |
|---|---|---|---|
| nvs | 0x9000 | 0x5000 (20 KB) | settings, kWh counters, pairing, hardware profile (`hw_act` / `hw_try`) |
| otadata | 0xe000 | 0x2000 | OTA slot selection |
| app0 (ota_0) | 0x10000 | 0x600000 (6 MB) | firmware |
| app1 (ota_1) | 0x610000 | 0x600000 (6 MB) | OTA slot, same size |
| spiffs (LittleFS) | 0xC10000 | 0x3E0000 (about 3.9 MB) | offline buffer (outbox) |
| coredump | 0xFF0000 | 0x10000 | crash dumps |

The table ends exactly at 16 MB. The app uses about 1.3 MB of its 6 MB slot, so there is room for growth and OTA.

**Do not move `nvs` / `otadata` / `app0`.** The Arduino upload writes `boot_app0` at **0xe000** and the sketch at **0x10000**
(hard-coded in the ESP32 core's `platform.txt`, not taken from the partition table). A table with the app anywhere else flashes
without an error and then never boots: `invalid magic byte (nothing flashed here?)`, `No bootable app partitions in the partition
table`; the app lands inside the NVS area. `firmware/tools/check_shared.py` checks this. The consequence is that NVS stays at the
usual 20 KB (4 usable pages, one blob up to about 4 KB). Settings use roughly half of it; the hardware profile is under 1.2 KB per
copy, and `hwprofile.cpp` refuses (`err: "storage"`) or rolls back instead of claiming a profile it could not store.

Switching an existing meter to this table (or recovering a meter that does not boot): **erase the whole flash once** (Arduino IDE:
Tools > *Erase All Flash Before Sketch Upload* > Enabled, upload, then set it back to Disabled; or `esptool erase_flash`). The meter
then has to be paired again. The LittleFS partition moves and is formatted (buffered readings are lost).

Reference core: Arduino-ESP32 **3.3.5** (used on the hardware). Also compiles on 3.3.12 and 2.0.17. Libraries: ArduinoJson 7.4.2, WebSockets 2.7.3. The only API that differs between the cores is the RGB LED call (`rgbLedWrite` on 3.x, `neopixelWrite` on 2.0.x), handled in `led.cpp`, plus the ESP-NOW receive-callback signature (handled in `mesh.cpp`).

## 11. Sketch layout

```
firmware/PowerMeterPZEM/
  PowerMeterPZEM.ino  setup(): init + start tasks; loop(): BOOT button + soft watchdog
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


## I2C faults: what the meter does when a device goes away

The I2C bus is shared by the rbAmp module(s), the relay board(s), the DS3231 clock and the SHT sensors (`i2cbus.cpp`, one
lock). Nothing needs a restart:

| Device | What happens when it disconnects / loses power | Recovery |
|---|---|---|
| rbAmp module | reads fail: after `sensor.timeout_s` (default 10 s) one `sensor_error` event and the configured action (run / hold / end session); with several modules one missing module fails the whole read | reads continue every cycle, the first good one sends `sensor_ok`. After 5 s of silence the module is forgotten and probed again every 5 s (`rbamp::begin()`): variant, CHANNELS table and clamp models are checked / written again. A restart of the module inside an energy period is detected from its uptime (period discarded, models written again) |
| relay board (MCP23017 / XL9535) | writes fail: one `relay_error` event; the other boards are still written | the state **and the pin directions** are written every 2 s and on every change, so a board that lost power or was plugged in after the start works again by itself (`relay_ok`). PCF8574: the byte is the state |
| SHT sensor | no value for that sensor | read again every poll cycle |
| DS18B20 | not found at start-up: searched for again every minute | — |
| a device holding SDA low (brownout in the middle of a byte) | every transaction would time out | **bus recovery** (`i2cbus.cpp`): after 3 failures that are not a plain NACK, or when SDA is low on an idle bus (checked every second), the Wire driver stops, SCL is clocked up to 9 times, a STOP is sent and Wire restarts; at most once per 2 s. A device that is simply absent (NACK) never triggers it. `info.i2c_recov` counts the recoveries |

Host tests: `firmware/tools/i2c_test` (the real `relays.cpp` and `i2cbus.cpp` against fake expanders / a fake Wire).
