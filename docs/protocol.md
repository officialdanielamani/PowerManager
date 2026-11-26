# Meter ⇄ Server Protocol (v1)

All times are **UTC epoch seconds**. All messages are JSON. Field names are short to keep payloads small on the ESP32.

## 1. Pairing

### 1.1 Server side

1. Admin clicks **[Add New Meter]**.
2. The server creates a pairing code: 8 characters from `23456789ABCDEFGHJKMNPQRSTUVWXYZ` (no 0/O/1/I/L), shown as `XXXX-XXXX`. It is valid for **10 minutes** and can be used **once**.
3. The page shows:
   - QR code
   - Server address, port, TLS on/off (from *Settings → Server*)
   - Pairing code
   - Countdown + [Regenerate]
4. The page polls `GET /meters/pairing/<code>/status`. When the status becomes `paired`, it redirects to **Configure Meter Device** for the new meter.

QR content (JSON, kept short):

```json
{"pm":1,"h":"power.example.com","p":443,"s":1,"c":"K7P29QXM"}
```

| Key | Meaning |
|-----|---------|
| `pm` | QR format version |
| `h` | host |
| `p` | port |
| `s` | 1 = TLS (https/wss), 0 = plain |
| `c` | pairing code (no dash) |

Manual entry on the device uses the same four fields: **Server address**, **Port**, **Secure**, **Pairing code**.

### 1.2 Device side

`POST {scheme}://{h}:{p}/api/v1/pair`

```json
{
  "code": "K7P29QXM",
  "device_id": "MTR23H4R",
  "fw": "0.1.0",
  "hw": "esp32s3-n16r8",
  "proto": 1,
  "caps": {
    "kind": "ac",
    "sensor": "pzem004t",
    "channels": 1,
    "relays": 1,
    "inputs": ["tamper"],
    "rtc": false
  }
}
```

`caps.kind` is the meter type: `"ac"` (PZEM-004T, sends `v a w pf hz e`) or `"dc"` (PZEM-017, sends `v a w e`: no power factor / frequency; V and A with 0.01 resolution). It is also sent in every `hello`, and the server sets the meter type from it (AC/DC badge, AC or DC price, DC display without PF/Hz). Missing = AC.

Responses:

| HTTP | Body | Meaning |
|------|------|---------|
| 200 | `{"secret":"<32B base64url>","ws":"/ws/device","time":1762170000,"config":{…}}` | Paired. The device stores `secret`, host, port, TLS in NVS. |
| 400 | `{"err":"bad_request"}` | Malformed |
| 404 | `{"err":"invalid_code"}` | Unknown / used / expired |
| 409 | `{"err":"already_paired"}` | This `device_id` is already active on the server (admin must remove it first) |
| 429 | `{"err":"rate_limited"}` | Too many attempts |

The server stores only a **hash** of `secret`.

## 2. Runtime WebSocket

`wss://{h}:{p}/ws/device`, with headers:

```
Authorization: Bearer <device_id>.<secret>
X-PM-Proto: 1
```

Close codes from the server:

| Code | Meaning | Device behaviour |
|------|---------|------------------|
| `4401` | Bad credentials (unknown device / wrong secret) | Stop retrying, show "Not paired – re-pair needed" on the local dashboard. Local admin and Wi-Fi are kept. |
| `4403` | Device was **removed** by the admin | **Factory reset** (same as the `remove` command), reboot into first-boot AP mode |

Before closing with `4401` / `4403` the server sends `{"t":"bye","code":4401|4403}`, because some WebSocket client libraries (including the one used on the ESP32) do not expose the close code. Devices act on whichever they see first.

The server keeps the "removed" marker for the `device_id` so a device that was offline during removal still gets `4403` when it next connects.

### 2.1 Envelope

```json
{"t":"<type>", "id":123, ...}
```

`id` is a message counter per connection (informational). Unknown `t` values are ignored (forward compatibility).

Other close codes: `1001` = server is shutting down (reconnect with backoff), `4000` = replaced by a newer connection of the same device (do not fight it; just reconnect normally).

### 2.2 Device → Server

| `t` | When | Body |
|-----|------|------|
| `hello` | First message after connect | `fw, proto, boot` (boot counter), `up` (uptime s), `tv` (time valid bool), `ts`, `cfg_ver`, `caps`, `state` |
| `tel` | Each report interval, on [Send Data Now], and when sending buffered data | see 2.4 |
| `evt` | A state change happens | see 2.5 |
| `state` | After any relay/session change, and in response to `get_state` | see 2.6 |
| `cfg_ack` | After applying a config | `ver`, `ok`, `err?` |
| `cmd_ack` | After a command | `ref` (copied from the `cmd`), `ok`, `err?` (`quota_exceeded`, `session_expired`, `tripped`, `unknown_cmd`) |
| `info` | Reply to `get_info` | `i2c_recov` (times a stuck I2C bus was freed since start), `ip`, `ssid`, `rssi`, `mac`, `heap`, `heap_min`, `psram`, `psram_free`, `fw`, `hw`, `hw_info` (config.h `HW_INFO`), `chip`, `flash`, `core`, `uptime`, `boot`, `outbox`, `time_src`, `stack` (free stack per task), `reset` (last reset reason, SAFE MODE), `clock` (meter clock, epoch; 0 = not set), `rtc` (DS3231 time; only with a DS3231, 0 = not readable), `sensor` / `kind` (AC/DC Power Sensor), `bat_v` + `bat_pct` when `BATTERY_ENABLE` is on, `temp_sensors` (serial number and value of each DS18B20) with `TEMP_ENABLE`; multi-channel: `clamps`, `sensor_boards`, `relay_board` |
| `ping` | Every 30 s | – (server replies `pong`) |

### 2.3 Server → Device

| `t` | When | Body |
|-----|------|------|
| `welcome` | Reply to `hello` | `time` (server UTC), `cfg_ver` (wanted), `last_seq` (highest telemetry seq stored) |
| `ack` | After storing `tel` | `last_seq`, `time` |
| `cfg` | Config changed or device `cfg_ver` is outdated | `ver`, `cfg` (see 3) |
| `cmd` | Admin action | `c`, `ref` (command number, echo it in `cmd_ack`) + params, see table below |
| `time` | Periodic (hourly) | `time` |
| `pong` | Reply to `ping` | `time` |

Commands (`cmd.c`):

| `c` | Params | Device behaviour |
|-----|--------|------------------|
| `relay` | `on: bool` | Set the wanted relay state. ON is **rejected** if quota exceeded, session expired or tripped. |
| `renew` | – | End the current session (`manual`), start a new one, clear quota/expired flags |
| `send_now` | – | Send `tel` right away |
| `reset_trip` | – | Clear the over-current latch |
| `get_state` | – | Reply with `state` |
| `remove` | – | Reply `cmd_ack`, close, then **factory reset**: wipe local admin, Wi-Fi, pairing, config, session, trip latch and buffer (AP password kept) → reboot into first-boot AP mode |
| `disable` | – | Server disabled the meter: end the session (reason `disabled`), relay OFF, refuse Relay ON (also locally), no new sessions. Persisted across reboots |
| `enable` | – | Undo `disable`. No new session starts; the relay stays off until `renew` |
| `get_info` | – | Reply with an `info` message (below) |
| `set_default` | – | Not a device command: the server makes a new default config and sends it as `cfg` |

Commands for an offline device are **queued** on the server and sent on reconnect (except `send_now`, which is dropped, and `relay`, which is replaced by the latest one).

### 2.4 Telemetry

```json
{
  "t": "tel", "id": 42,
  "items": [
    {
      "seq": 10233, "ts": 1762170000, "tv": 1,
      "ch": [ {"i":0, "v":230.1, "a":4.21, "w":960.2, "pf":0.98, "hz":50.0, "e":12.345} ],
      "s": {"sid":17, "kwh":1.2345}
    }
  ]
}
```

| Field | Unit / meaning |
|-------|----------------|
| `seq` | Strictly increasing per device, survives reboot (kept in NVS, stored in blocks so it is not written every time). The server stores each `seq` once → resending is safe. After a factory reset `seq` starts again at 1; the server handles this because every (re)pairing starts a new *pairing generation*. |
| `ts` | Time of the reading |
| `s` | Session progress: `{"sid": 17, "kwh": 1.2345, "kwh_t": 1.2001}` (`kwh_t` = tared, only when a tare is set; that is the value the kWh limit uses with `tare.limits`) (kWh used in the current session). Keeps the server's "session used" current; `state` is only sent on changes. |
| `tv` | 1 = clock was valid. 0 = the device had no valid time; then `ts` holds **uptime seconds** and the item also has `boot`. The server fixes the time using the `hello` (`up` + `ts` of that boot) when possible, otherwise marks the item `time_estimated`. |
| `v` V, `a` A, `w` W, `pf` 0–1, `hz` Hz, `e` kWh | PZEM values. `e` is the PZEM's total energy register. |
| channel missing / `null` values | No reading (e.g. AC lost: the PZEM measurement side is powered from AC) |
| `rh` | With SHT sensors: humidity %RH of the same sensors as `t` (`null` for DS18B20) |
| `t` | Only with temperature sensors (firmware `TEMP_ENABLE`), on the first item after each new round of readings (every `TEMP_READ_MS`): `[24.5, 31.0]` °C for T1, T2 …; `null` = that sensor could not be read |
| `se` | Only when the item has no reading: `no_reply` (the sensor did not answer: sensor/wiring fault or no AC) or `bad` (garbled reply / CRC error). The server then shows "no current reading" instead of the old values; no `se` and no channel = no AC |

**Ordering rule:** telemetry items and events share **one ordered queue** on the device (the offline buffer). Nothing bypasses it, not even a live event, so `seq` arrives in increasing order and an `ack` with `last_seq` safely confirms everything up to it.

Items are sent oldest first, at most **50 items per message**. After sending, the device waits for the `ack` (resends after 5 s without one). It deletes items from its buffer only after an `ack` with `last_seq ≥ seq`.

### 2.5 Events

```json
{"t":"evt","id":43,"ts":1762170000,"tv":1,"e":"overcurrent","ch":0,"val":10.6}
```

| `e` | Meaning |
|-----|---------|
| `overcurrent` | A > `amp_max` → relay OFF, latched |
| `quota` | Session kWh ≥ `kwh_max`. `action`: `end` → followed by `session_end` reason `quota` (the **server also closes the session record on this event**, in case `session_end` is late); `hold` → relay OFF, session stays open; `warn` → power stays on (sent once per session). |
| `session_end` | Session ended (`reason`: quota/expired/manual). Sent **once** per session: renewing a session that a limit already ended does not send another |
| `session_start` | New session (`sid`, `auto`: bool) |
| `tamper_open` / `tamper_close` | Panel switch. With Tamper Protection on, `tamper_open` carries `action` |
| `sensor_error` / `sensor_ok` | No valid reading for `sensor.timeout_s`: `{"kind": "no_reply"\|"bad", "after_s": 10, "action": "hold"}`; `sensor_ok` = first valid reading again |
| `session_end` summary | The meter adds `"sum": {"n": 3600, "v": [min, max, avg], "a": [...], "w": [...]}` (shared multi-channel session: total current / power of all channels) |
| `next_session` | The meter carried out a **Schedule Next Session** itself (`at` = the scheduled epoch, `ch`? in Independent mode). It may arrive long after it happened (the meter was offline: events are buffered). The server then copies the scheduled values into its own configuration without sending anything; if it holds no such schedule (cleared or changed meanwhile) it sends the meter its current configuration, which puts the meter back |
| `quota_cleared` | The kWh limit was raised (or removed) while the session was held at the old limit: the session continues (`kwh`, `max`, `ch`?) |
| `temp_high` / `temp_ok` | A temperature sensor went above `temp.max`: `i` (sensor, 0 = T1), `c` (°C), `max`, `action`; `temp_ok` = every sensor back 5 °C below the limit (`i`, `c`), or the limit was switched off (`disabled`) |
| `voltage_out` / `voltage_ok` | Voltage left / returned to the operating range: `v`, `min`, `max`, `dir` (`low`/`high`), `action` |
| `ac_lost` / `ac_restored` | V < 50 V or PZEM not responding for 3 reads / back to normal |
| `trip_reset` | Over-current latch cleared (`src`: server/local, `by`: username) |
| `offline_relay_off` | Relay switched off because the server was unreachable longer than the limit |
| `server_lost` / `server_back` | Device's own view of when it lost / regained the server (`ts` from the device clock). Lets the server see the real offline time even when the server itself was down. |
| `relay` | Relay changed (`on`, `src`: server/local/rule) |

Events are buffered the same way as telemetry (they share the `seq` counter) so none are lost while offline.

Disconnect / reconnect notifications are produced **by the server** (from the WS connection + ping timeout), not by the device.

**Conditions present at boot:** if the panel is open, AC is missing or the trip latch is set when the device starts, it sends the matching start event (`tamper_open`, `ac_lost`, `overcurrent` with `boot:1`), so no incident is missed across a reboot. The `state` in `hello` (`tamper`, `ac`, `tripped`) is also used by the server to close incidents whose end event was lost.

### 2.6 State

```json
{
  "t":"state","id":44,
  "relay":[{"i":0,"on":false,"want":true,"block":"quota"}],
  "tripped":false, "tamper":false, "ac":true,
  "sess":{"sid":17,"start":1762128000,"kwh":2.000,"max":2.0,"status":"quota","next_reset":1762189200}
}
```

`block` ∈ `null | tripped | quota | expired | offline | ac_lost`. `status` ∈ `active | quota | expired`. `sess.over: true` = kWh limit passed with `kwh_action: warn` (power still on). `sess.ended: true` = `session_end` already sent for this session. `status: ended` + `sess.end_reason` (`voltage`/`tamper`/`sensor`/`disabled`) = ended early by a protection (relay OFF until renewed). `block` can also be `ended`, `voltage`, `tamper`, `disabled`. `disabled: true` = disabled by the server. `volt_out: true` = voltage currently out of range. `block: sensor` = relay OFF by the sensor timeout (`hold`); the state then also has `"sensor_err": "no_reply"|"bad"` (only while there is one).

## 3. Config document

The server sends the whole config every time (no partial updates).

```json
{
  "ver": 7,
  "cfg": {
    "report_s": 60,
    "read_s": 60,
    "offline_off_min": 3,
    "sess": {
      "mode": "daily",
      "daily_utc_min": 1020,
      "dur_min": 1440,
      "auto": true
    },
    "volt": {"min":200, "max":250, "action":"hold"},
    "tare": {"a":0.012, "w":1.6, "limits":true},
    "tamper": {"protect":true, "action":"end"},
    "sensor": {"timeout_s":10, "action":"hold"},
    "ch": [ {"i":0, "kwh_max":2.0, "kwh_action":"end", "amp_max":10.2} ]
  }
}
```

| Field | Range | Default | Notes |
|-------|-------|---------|-------|
| `report_s` | 10–3600 | 60 | Send buffered readings to the server every … s (events go right away) |
| `read_s` | 5–`report_s` | = `report_s` | Record a reading every … s. Example: `read_s` 10 + `report_s` 60 = 6 readings sent together once a minute. |
| `offline_off_min` | 0–1440 | 0 | 0 = disabled |
| `sess.mode` | `weekly` / `daily` / `duration` / `none` | `none` | `none` = no time limit |
| `sess.daily_utc_min` | 0–1439 | – | Local reset time converted to UTC by the server. Example: 01:00 at UTC+08:00 → 17:00 UTC → `1020` |
| `sess.wk` | list of 1–7 integers, 0–10079 | – | `weekly` mode: reset times as **UTC minutes of the week**, Monday 00:00 UTC = 0 (Wednesday 18:30 UTC = 2·1440 + 1110). The server converts the chosen days + times of its time zone (using the offset of the moment it builds the config, like `daily_utc_min`). The session ends at the first of these times after it started (`src/weekly.h`, host test `tools/weekly_test.cpp`). The meter rejects `weekly` without a time |
| `sess.dur_min` | 1–525600 | – | Duration mode; UI enters days/hours/minutes |
| `sess.auto` | bool | false | |
| `next` | object | absent | **Schedule Next Session** of the whole meter (single-channel meter, or Shared mode of a multi-channel meter): `{"at": epoch, "kwh_max", "kwh_action", "amp_max"?, "sess": {…as `sess`}}`. At `at` (epoch, the **meter's clock**: NTP or RTC) the values replace the current ones (`amp_max` only for a single-channel meter; in Shared mode only `kwh_max`, `kwh_action` and `sess`), the running session ends and, with `sess.auto`, a new one starts on them (end reason `next`; without `auto` the session just ends, relay off until Renew). Then the meter drops the schedule, remembers its `at` in NVS (a configuration that still carries that `at` is not run again) and sends the event `next_session`. **It runs while the meter is offline**; without a valid clock it waits for one. A schedule that is already past when the meter receives it (or when its clock becomes valid) runs at once.|
| `ch[].next` | object | absent | The same for one channel of a meter in Independent mode (`amp_max` included) |
| `ch[].kwh_max` | 0–100000 | 0 | 0 = unlimited |
| `ch[].kwh_action` | `warn` / `hold` / `end` | `hold` | What happens at `kwh_max`. **warn** = *Session run as usual* (`quota` event once + `sess.over`, power stays on). **hold** = *Off relay, but session continue* (relay OFF until the session ends at its time limit or is renewed). **end** = *End session* now (relay OFF until renewed). |
| `volt.min`, `volt.max` | 0–400 V | 0 | Operating voltage range; 0 = no limit on that side; min < max |
| `volt.action` | `run` / `hold` / `end` | `hold` | Out of range (3 readings in a row): **run** = event only; **hold** = relay OFF until back in range (10 readings in a row); **end** = end the session (`session_end` reason `voltage`) |
| `tare.a`, `tare.w` | 0–5 A, 0–500 W | 0 | The meter's own consumption on the measured line |
| `tare.limits` | bool | true | Use tared values for the kWh limit (session kWh − tare W × time with readings) and max current (A − tare A). Voltage is never tared |
| `tamper.protect` | bool | false | Off: panel open/close is only reported |
| `tamper.action` | `run` / `hold` / `end` | `hold` | With protection on, when the panel opens: **run** = event only; **hold** = relay OFF while open; **end** = end the session (reason `tamper`) |
| `sensor.timeout_s` | 0, 3–3600 s | 10 | No valid reading for this long = sensor error (`sensor_error` event); 0 = off. A power cut also stops the PZEM from answering |
| `sensor.action` | `run` / `hold` / `end` | `hold` | **run** = event only; **hold** = relay OFF until readings return (`sensor_ok`); **end** = end the session (reason `sensor`) |
| `temp.max` | 0–125 °C | 0 | Over-temperature (any temperature sensor); 0 = off |
| `aux` | list | `[]` | AUX relays: name + automation (manual / schedule / event), see docs/multichannel.md § AUX relays |
| `summary` | bool | false | "Core data only": no interval readings (telemetry only on Get Data Now); each session is reported by its `session_end` summary |
| `tz_min` | −840…840 | 0 | UTC offset of the display time zone in minutes (AUX schedules run in local time) |
| `temp.action` | `run` / `hold` / `end` | `hold` | **run** = event only; **hold** = relay(s) OFF until it cools down (`temp_ok`); **end** = end the session (reason `temp`) |
| `ch[].amp_max` | 0–100 | 0 | 0 = disabled; must not exceed the sensor/relay rating set in caps |

The device checks every value. If anything is out of range it rejects the **whole** config (`cfg_ack ok:false err:"<field>"`) and keeps the old one.

## 4. Timing

| Item | Value |
|------|-------|
| Control loop (read PZEM, check limits) | 1 s |
| `ping` message (device) | every 30 s; reconnect if no `pong` within 10 s |
| Server marks device offline | No message/ping for 75 s |
| Reconnect backoff | 2, 4, 8, 16, 32, 60 s (max), with random jitter |
| Server `time` push | hourly + in every `ack` |

## 5. Versioning

- `proto` = 1 for this document. A breaking change bumps it; the server supports the current and previous version.
- New optional fields may be added without a bump; both sides ignore unknown fields.

## 6. Mesh (gateway-relayed leaves)

See [mesh.md](mesh.md). Extra messages on a **gateway's** WebSocket:

| Direction | Message | Meaning |
|---|---|---|
| gateway → server | `{"t":"via","d":<leaf>,"n":<uint>,"h":<hmac>,"m":"<json>"}` | One protocol message of leaf `d`, signed by the leaf |
| server → gateway | same shape | One protocol message for leaf `d`, signed by the server (`n` = ms counter) |
| gateway → server | `{"t":"via_pair","body":{…pair body…}}` | Pair a leaf (body as for `/api/v1/pair`) |
| server → gateway | `{"t":"via_pair_res","d":<leaf>,"code":<http status>,"resp":{…}}` | Pairing answer for the leaf |
| server → meter (direct only) | `{"t":"link_key","key":"<32 hex>"}` | The meter's mesh signing key (also in the pairing answer as `link_key`) |

`h` = first 128 bits (hex) of HMAC-SHA256(link_key, `"<d>|<n>|<m>"`). The server drops messages with a bad signature or a counter
that is not higher than the last one; the leaf does the same for downlink messages.

## 7. MQTT link (optional)

The same protocol can travel through an MQTT broker instead of the `/ws/device` WebSocket (firmware `NET_MQTT`,
server *Settings → MQTT*). Meters always connect with **MQTT over WebSocket** (`ws://` / `wss://`, sub-protocol
`mqtt`, e.g. behind Cloudflare); the server connects with MQTT over WebSocket or plain TCP. MQTT 3.1.1, QoS 0 (the
protocol has its own acknowledgements and resends).

| Topic | Direction | Content |
|---|---|---|
| `<prefix>/<DEVICE_ID>/up` | meter → server | one signed protocol message |
| `<prefix>/<DEVICE_ID>/down` | server → meter | one signed protocol message |
| `<prefix>/<DEVICE_ID>/status` | meter → server | `online` / `offline`, retained (`offline` is the meter's last will) |

`<prefix>` defaults to `powermanager`. Every message on `up` / `down` is wrapped like a mesh message:

```json
{"n": 1234, "h": "<32 hex>", "m": "{\"t\":\"hello\", ...}"}
```

`h` = first 128 bits (hex) of HMAC-SHA256(link_key, `"<DEVICE_ID>|<n>|<m>"`), with the meter's `link_key` from pairing.
Somebody who only has the broker login can therefore neither fake a meter nor command one.

- Uplink `n` is the meter's stored counter; the server drops anything not higher than the last accepted one.
- Downlink `n` is the server clock in ms; the meter drops anything not higher than the last one, and (with a valid
  clock) anything older than 10 minutes.
- There is no token in `hello`: the signature is the authentication. A disabled / unknown meter gets `bye`.
- `{"t":"rehello"}` (server → meter): the server has no session for this meter (e.g. it restarted); the meter sends
  `hello` again.
- A meter silent for 75 s, or whose `status` becomes `offline`, is shown offline.

**Auto link.** The pairing answer carries the broker for the meter (or `null`):

```json
"mqtt": {"enabled": true, "host": "mqtt.example.com", "port": 443, "tls": true, "path": "/",
         "user": "", "pass": "", "prefix": "powermanager"}
```

**Fallback.** If the broker cannot be reached for `MQTT_FALLBACK_MS` (5 min) the meter uses the WebSocket, and tries
the broker again after `MQTT_RETRY_MS` (30 min). With `NET_WEBSOCKET 0` it keeps trying the broker.

**New commands** (`cmd`, acknowledged with `cmd_ack` as usual):

| `c` | Arguments | Effect |
|---|---|---|
| `set_mqtt` | the `mqtt` object above (`enabled: false` = WebSocket only) | save, then reconnect over the new link |
| `set_wifi` | `ssid`, `pass` | try the new Wi-Fi for 20 s; saved only if it connects, otherwise the old one is kept |

After `set_wifi` the meter reports event `wifi_change` `{"ok": true|false, "ssid": "..."}`. The server wipes the
password from the stored command once it is acknowledged. `info` now also carries `transport` (`mqtt` / `websocket`)
and `mqtt` (`host:port`); `hello.caps` has `mqtt` and `ws` (what the firmware was built with).

## 8. AUX automation

Config `aux[i]`:

- `sched`: up to 14 entries `{"on", "start", "end"}` (minutes of the local day) with either `"days"` (bit 0 = Monday
  … bit 6 = Sunday) or `"date": [year, month, day]` (year 0 = every year). First match wins.
- `events` (event mode): **1 to 3 conditions, OR-ed**, each `{"src", "idx", "cmp", "value", "running" | "active"}`, and `ev_on` (bool): while **any** condition is true the relay is ON (`ev_on` true) or OFF; otherwise the opposite. Unknown conditions (sensor missing, no reading) count as false, but when nothing is true and one is unknown the last state is kept. Hysteresis is per condition. The older single `event` object (with its own `on`) is still accepted by the meter (= one condition, `ev_on` = its `on`); the server sends `events` / `ev_on`. More than 3 conditions, or event mode without a condition, is refused (`aux.event`).
- condition `src`: `temp`, `hum`, `amp`, `watt`, `kwh`, `session`, and new `tamper`, `trip`, `volt`, `wifi`, `server`, and (from 1.1.0, §12) `reboot` = "Wait for Reboot": active during the countdown of a hardware profile change.
  The new ones are on / off conditions: `"active": true|false` (`wifi` / `server` are active while the link is
  missing). `event.idx`: channel / sensor, `255` = all channels together (amp, watt), `254` = any channel (amp,
  watt, kwh, session, trip).
- `hello.caps.hum` = true when an SHT (humidity) sensor is configured.

## 9. Backup Wi-Fi

- `set_wifi` with `"slot": "backup"`: `ssid`, `pass` are saved as the backup network without a test; an empty
  `ssid` removes it. (Without `slot` the command changes the main network as before.)
- Event `wifi_backup` `{"on": true|false, "ssid": "..."}`: the meter connected to the backup network / is back on
  the main one.
- `info`: `wifi_net` (`main` / `backup`) and `wifi2` (name of the backup network, if any).
- Without Wi-Fi the meter tries main, backup, main … every 30 s. On the backup it scans for the main SSID every
  `WIFI_MAIN_CHECK_MS` (5 min; doubled up to 1 h after each return that did not hold) and reconnects to it when seen.

## 10. UPS input and battery % (`hello.caps.ups`, `hello.caps.battery`)

- Firmware `UPS_ENABLE 1`: a signal input (`PIN_UPS`, active level `UPS_ACTIVE_LEVEL` LOW or HIGH, debounced
  `UPS_DEBOUNCE_MS`). Events `ups_on` (also with `"boot": 1` when already active at start-up) and `ups_off`; state
  field `ups`. The server opens / closes a historical event "On UPS power" and sends the *UPS power* notification.
- AUX `event.src` `ups` (`"active": true|false`) and `bat` (battery % remaining from `BATTERY_ENABLE`, `cmp` /
  `value`, 2 % hysteresis). Offered by the server only when `caps.ups` / `caps.battery` are true.

## 11. Supported firmware

The server works with meter firmware **1.2.0 or newer** (both sketches; weekly session resets and Schedule Next Session
of §3, the hardware profile of §12 ...). Older firmware is not supported: the
meter's Configuration page and the Firmware page show that it must be updated. What a meter can do is taken from
`hello.caps` (parts switched on in its `config.h`), not from its version number.

## 12. Hardware profile (`hello.caps.hw_cfg`) — design, firmware / server 1.1.0

Status: **implemented** on the server (`services/hwprofile.py`, `tools/sim_meter_mc.py`) and in the rbAmp firmware (`src/hwprofile.cpp`; PC-tested, not yet tried on real hardware). Only the rbAmp sketch (`PowerMeterRBAmp`) takes part; the PZEM
sketch keeps its compile-time layout until a later version.

What the wiring looks like (rbAmp modules, relay boards, which input / clamp / relay each channel uses) can be
changed from the server without recompiling. The compiled tables of `config.h` (`RBAMP_BOARDS`, `RELAY_BOARDS`,
`CHANNELS`) become the **default profile**. Pins, enable flags and the maximums (`MC_MAX_CH`, 8 rbAmp boards,
32 relays, 8 relay-table entries) stay compile-time. A channel's relay is a relay board (by its stable `id`, shown as a letter) and an output on it
("Board A relay 2"), not a number across all boards; the meter derives the relay number across boards (lowest
`id` first) that AUX relays, `state.relay` and the AUX events keep using. `config.h` counts its relay boards by
position (first entry = Board A): the generator renumbers a profile with a gap: they follow the hardware design.

### 12.1 Profile document

```json
{"ver": 7,
 "boards": [80, 81],
 "relays": [{"id": 2, "drv": "mcp23017", "addr": 33, "n": 8, "hi": 1}],
 "ch": [{"b": 1, "i": 1, "a": 10, "rb": 2, "ro": 1},
        {"b": 1, "i": 2, "a": 10, "rb": 2, "ro": 2},
        {"b": 2, "i": 1, "a": 30, "rb": 2, "ro": 3}]}
```

The example has only **Board B** (`id` 2): Board A was removed and Board B stayed Board B.

| Field | Meaning | Rules (same on server and meter) |
|---|---|---|
| `ver` | Profile version, raised by the server on every change | integer ≥ 1 |
| `boards` | I²C address of each rbAmp module, board 1 first | 1–8 entries, each 0x50–0x57, no duplicates |
| `relays[]` | One entry per relay board (at most 8). `id` 1–8 = the board's letter (1 = Board A … 8 = Board H); it is **kept**: removing Board A leaves Board B as Board B, a new board takes the lowest free id. The list is in ascending `id` order (the server sorts it); relay numbers across boards (AUX relays) follow that order | `id`: unique, 1–8; `drv`: `mcp23017` / `xl9535` (≤ 16), `pcf8574` (≤ 8), `gpio` (≤ pins listed in `RELAY_GPIO_PINS`); `addr`: 0x20–0x27 (`pcf8574` also 0x38–0x3F, `gpio`: 0); `hi`: 1 = active high; at most 32 relays in total; no two entries on the same address |
| `ch[]` | One entry per measured channel, CH1 first | 1–`MC_MAX_CH` entries; `b` = board number (1..number of boards), `i` = input 1..7, `a` = clamp model in amps (`0` keep, 5, 10, 20, 30, 50, 60, 100), `rb` = the `id` of a relay board (1 = Board A, 2 = Board B …; 0 = no relay; it must exist in `relays`), `ro` = output on that board (1..its `n`; 0 when `rb` is 0); no two channels on the same `b`+`i`, no (`rb`, `ro`) used twice |

These are the rules of the `static_assert`s in `hwmap.h` / `relaymap.h`, moved to run time. They are written once
(firmware: one validator function that the PC tests also run; server: `services/validation.py`). Relays that no
channel uses are AUX relays as before.

### 12.2 Capability and state

- `hello.caps.hw_cfg` = profile format version (`1`). The server offers **Update Device I/O** only when it is present.
- `hello.hwp` = `{"ver": 7, "state": "active", "profile": {…}}` (the profile in use, format 12.1, without `ver`). `state` ∈ `default` (compiled tables, nothing sent yet), `active`
  (a server profile in use), `trial` (booted with a new profile, not confirmed yet), `pending` (stored, reboot
  countdown running).

> **rbAmp and I2C scans:** an rbAmp SCT-UI7 ACKs every address (tested alone on the bus at 50 / 20 / 100 kHz), so an ACK does not prove a part is there. `i2c_scan` therefore lists every ACKing address only up to 16; with more ACKs it lists the addresses that also return a byte when read (register 0x03) and sets `res.flood = true`. The rbAmp itself is confirmed by `PRODUCT_ID` (`res.rbamp`).

> **config.h wins over a stored profile (1.1.0):** the meter stores a fingerprint of its compiled default (`hw_dh` in NVS) with every profile it keeps. When a changed `config.h` is flashed (e.g. a clamp 30 A -> 20 A) without erasing the flash, the stored profile no longer belongs to it: the meter drops it (and any trial) and runs `config.h`; its next hello has `hwp = {state: "default", ver: 0, reset: "config", profile}`. The server then forgets its wanted profile (does not send it again) and takes the reported profile as current. A profile stored by an older firmware (no fingerprint) is kept and tied to the config.h of that moment. Erasing the flash (state `default` without `reset`) still makes the server send its stored profile again.

> **Which clamp value is true:** the module itself. `i2c_scan` returns for every rbAmp `modules[].ct` = the clamp rating (A) the module reports it uses (read back from `APPLIED_CT`); the Update Device I/O page shows it next to every input (red when the profile says something else). After a **live** clamp change the meter sends a fresh hello (new `caps.ratings`) and the server also updates `caps.ratings` from the profile at once; the Configuration page's "clamp N A" comes from `caps.ratings`.

> **Battery in the state (1.1.0):** `state.bat` = battery percentage (integer, only with `caps.battery`), re-sent when it moved by 2 % or more; the server uses it for the "battery below X %" notification.

### 12.3 Commands (server → meter)

| `c` | Params | Behaviour |
|---|---|---|
| `hw_profile` | `ver`, `profile`, `mode`, `delay_s` | `mode: "live"` = only clamp models differ from the active profile: written to the modules at once, no reboot. `mode: "reboot"` = anything else: store as the **trial** profile and restart after `delay_s` (default 60, 10–600). The meter checks the mode against its own active profile and rejects a wrong one with `err: "mode"`. It validates the profile again (`err: "invalid"`, `detail`). Replaced by a newer `hw_profile` while a countdown runs |
| `hw_cancel` | – | Cancel a running countdown; the stored trial profile is dropped |
| `i2c_scan` | – | Probe addresses 0x08–0x77 and answer with `res: {"found": [32, 80, ...], "rbamp": [80]}` in `cmd_ack`. Addresses that identify as rbAmp (`HW_VARIANT` register) are listed in `rbamp`. The control loop waits for the bus meanwhile (the existing I²C lock) |
| `rbamp_addr` | `from`, `to` | Change one module's address (two-phase commit as `rbamp addr` on serial). Preconditions checked by the meter: `from` answers and identifies as rbAmp, `to` (0x50–0x57) does not answer, no session is running (`err: "busy"`). Afterwards the meter re-probes `to` and answers `res: {"ok": true}` or `err: "no_reply"` |

All are answered with `cmd_ack` (extra fields `res` = result object, `detail` = short text for an error); `hw_profile` adds `res: {"state": "live"|"pending", "in_s": 60}`. Commands for an
offline meter are queued like other commands, except that a newer `hw_profile` replaces an older queued one and
`i2c_scan` / `rbamp_addr` are dropped (they need someone watching).

### 12.4 Reboot sequence (`mode: "reboot"`)

1. `cmd_ack` (`state: pending`, `in_s`), event `hw_reboot_pending {ver, in_s}`. While the countdown runs, state
   `hwp.state = "pending"` and the AUX event source **`reboot`** ("Wait for Reboot", `active` true) is on, so a
   relay can be linked to it as a warning. The countdown survives nothing: a power cut before zero drops it and
   the old profile stays active.
2. At zero: every running session ends with reason `hw_change`, kWh counters and state are saved, relays go OFF,
   the meter restarts. Relays are OFF after every start anyway, so the warning relay ends with the countdown.
3. The meter boots with the trial profile and runs the **confirm check** (below).

### 12.5 Confirm check and rollback

- A trial profile becomes `active` only when **every** listed rbAmp (`boards`) and relay board (`relays`, I²C
  drivers) answers within `HW_CONFIRM_MS` (60 s, config.h option) of the start. The server link is **not**
  needed, so a Wi-Fi problem never causes a rollback.
- Success: event `hw_active {ver}`, `hwp.state = "active"`, the previous profile is kept as the rollback copy.
- Failure: the previous profile is restored and the meter restarts once more. Event `hw_rollback {ver, reason,
  addr?}` (`reason`: `no_reply` with the missing address, `invalid`, `reset`). A reset or crash before the
  confirmation counts as well: after the trial profile has started **twice** without being confirmed, the third start rolls it back (`reason: "reset"`), so there is no boot loop.
- If the previous profile itself is the compiled default, rollback = back to the default.
- The server marks the profile **Rolled back** and sends a *Meter status* notification with the reason. The wanted
  profile is not re-sent automatically; the user has to change it and save again.

### 12.6 Storage and defaults

- NVS keys (namespace `pm`): `hw_act` (the confirmed server profile; it also *is* the rollback copy, because it is only
  replaced at confirmation), `hw_try` (trial / pending profile), `hw_boots` (unconfirmed starts with the trial profile),
  `hw_note` (a rollback to report after the restart). Profiles are stored as JSON (below 1.2 KB). NVS is the usual 20 KB:
  when it does not take a profile, `hw_profile` is answered `err: "storage"` (nothing changes), and a trial profile that cannot
  be stored at the confirmation is rolled back (`reason: "storage"`).
- A live clamp change is stored straight into `hw_act` and applied to the modules at once; `caps.ratings` /
  `amp_rating` on the server are refreshed with the next `hello`.
- Factory reset and a `FW_TYPE` change clear all of them (back to the default profile). The server keeps its
  profile and re-sends it when `hello.hwp.state` is `default` and the server's profile is newer than `ver` 0.
- `hello` reports `hwp` (with the profile in use, so the server can pre-fill its page); `info` lists it too.
- `hello.caps.hw_limits` (optional) = limits of this build the server cannot know, e.g. `{"gpio": 2}` (relay pins in `RELAY_GPIO_PINS`).
- A new channel count that arrives in `hello` together with `hwp.state` `trial` / `active` / `pending` (or `default` right after a rollback) is **not** treated as other hardware: the meter is not archived (see multichannel.md, "Changing the hardware on a meter").

### 12.7 Effects on data

- kWh counters, limits and history belong to the **channel number**, not to the physical input: moving a clamp
  to another input keeps its counter.
- Channels above the new count are removed on the meter; the server **archives** them (history stays, they are
  hidden from the live view and from AUX / viewer links).
- Relays removed by a new profile that an AUX schedule / event still uses: the server refuses to save
  (validation names the AUX entry), the meter drops such entries if it receives them anyway.
- Config (`cfg`) for channels that did not exist before arrives after the first start with the new profile; until
  then they use the defaults.

### 12.8 Server side

- One profile per meter, stored as JSON on the device record, with `hw_ver` (wanted) and `hw_ack` (state last
  reported: `default` / `active` / `trial` / `pending` / `rolled_back`, reason). Column added in
  `migrations.py`.
- Pages (admin, and a manager for their own meters): Firmware → **Update Device I/O** → select an online meter that
  has `caps.hw_cfg`. Sections: rbAmp boards, relay boards, channel table (clamp dropdown), with **[Scan Address]**,
  **[Update Clamp Model]** (live mode only) and **[Save & Apply]** (reboot mode, with the countdown and **Cancel**).
  The rbAmp address change is a wizard ("connect only this module"), the expander addresses are chosen in a
  dropdown and checked against the scan (found / not found).
- **Generate config.h** can start from a meter's stored profile ("Start from the current profile of …"), so a
  later re-flash keeps the changes. It writes `RBAMP_BOARDS`, `RELAY_BOARDS`, `CHANNELS` as before.
- Meter configuration, history and notifications are unchanged; the new events (`hw_reboot_pending`,
  `hw_active`, `hw_rollback`) appear in the meter's event list and as *Meter status* notifications.

## 13. Zero, Tare and Calibration (per channel, all meters) — design, firmware / server 1.1.0

Status: **Zero and Tare are implemented** (server, both firmwares, simulators; PC-tested, not yet tried on real hardware). **Calibration is designed here and held** (not implemented yet).
Every value belongs to **one channel**: a single-channel meter has channel 0, on a multi-channel meter each channel has its own
zero / tare (independent of the others, also in Shared session mode).

| | What it is | How it is made | Where it acts |
|---|---|---|---|
| **Zero** | the sensor's offset with **no load** (clamp / shunt noise floor, ADC offset) | sensor check with the load disconnected | **on the meter**, right after every sensor read: all readings, limits, energy and history are already corrected |
| **Tare** | a **load that is connected and should count as 0** (a charger, a fixture, the meter's own supply) | sensor check with that load connected | server views (display, history, CSV, cost) and the **meter's limits** (max current, session kWh) |
| **Calibration** *(held)* | gain / linearity of the sensor against a reference meter | zero + 1 to 3 known loads | on the meter (correction curve) |

### 13.1 Sensor check (meter)

A sensor check measures the **raw** values of one channel (before zero and tare, so repeating it never compounds) and returns
statistics; the server decides what to do with them. Command (server → meter):

`{"t":"cmd","c":"sensor_check","ref":9,"ch":0,"n":10}` — `ch` channel (default 0), `n` readings to collect (10–60, default 10).

- The meter acknowledges at once (`cmd_ack`; `err`: `bad_channel`, `busy` = a check is already running, `no_sensor` = the sensor is
  not answering) and collects **n valid readings, at most one per second, so a check takes at least 10 s** (n readings = n seconds).
  A failed read does not count and is not retried faster; the check ends with `ok: false, why: "sensor"` after 60 s without enough
  valid readings. Only one check runs at a time; a restart or a new profile cancels it.
- The result is an **event** (it arrives seconds later and must not be lost offline):
  `{"e":"sensor_check","ch":0,"n":10,"secs":10,"a":[mean,min,max],"w":[mean,min,max],"v":[mean,min,max],"ok":true}`
  (`ok: false` with `why`: `sensor`, `cancelled`). Values are the raw sensor values (A, W, V).
- The relay and sessions are **not** touched by a check: the user connects / disconnects the load; the meter just measures.

### 13.2 What the server checks (it owns the thresholds, so they can change without a firmware update)

A check is **acceptable** when `n ≥ 10` and `secs ≥ 10` and the readings are **stable**: for current `max − min ≤ max(X A,
3 % of the mean)` with X = twice the smallest tare of the clamp (0.1 A up to 30 A, 0.3 A up to 50 A, 0.6 A up to 100 A, 1 A above; a clamp is noisier at small currents), for power `max − min ≤ max(3 W, 5 % of the mean)`. The meter counts a reading below 0 (sensor noise with no load, e.g. -1.4 W) as 0 in the check: zero and tare are never negative.
Additionally:
- **Zero**: the mean must be small: `a ≤ 1.0 A` and `w ≤ 100 W` — otherwise "a load is still connected".
- **Tare**: the load must be measurable: `a − zero.a ≥` a minimum that depends on the clamp rating: 0.05 A up to 30 A, 0.15 A up to 50 A, 0.3 A up to 100 A, 0.5 A above (bigger clamps are noisier at small currents), and not above the sensor rating.
The page shows the numbers and why a check failed; **Set zero / Set tare** is only offered for an acceptable check.

### 13.3 Stored values and config

The server keeps `Device.calib` = `{"<ch>": {"zero": {"a", "w", "at"}, "tare": {"a", "w", "at"}, "by": "<user>"}}` (`at` = epoch). It is
sent in the config, per channel: `cfg.ch[i].zero = {"a": 0.012, "w": 1.6}` and `cfg.ch[i].tare = {"a": 0.4, "w": 85}` (absent or
zeros = none), plus `cfg.tare_limits` (bool, default true) for **all** meters. Setting or clearing a value raises `cfg_ver`; the meter
stores them with its config. The earlier single-channel `cfg.tare {a, w, limits}` is gone (firmware 1.1.0 and newer only).
Changing the hardware profile keeps the values of channels that keep their number; channels that no longer exist lose theirs.

### 13.4 Zero on the meter

After every read of channel *c*: `a' = max(0, a − zero.a)`, `w' = max(0, w − zero.w)`; with `a' == 0` also `w' = 0`. Power factor,
voltage and frequency are not changed. rbAmp: the energy of a period is computed from the corrected average power, so the noise
floor is not accumulated. PZEM: the energy register is the source of truth (D8) and cannot be corrected; only current / power are.

### 13.5 Tare on the meter and on the server

Tare does not change what the meter measures or records: raw (zero-corrected) values stay raw in the history; tared values are
computed when displayed or exported (as the single-channel tare did before), **per channel**:
`a_t = max(0, a' − tare.a)`, `w_t = max(0, w' − tare.w)`, `kWh_t = max(0, kWh − tare.w × time)`; voltage is never tared.
With `cfg.tare_limits` the meter uses the tared values for its limits, **per channel**: max current (`a_t > amp_max`) and the session
kWh limit (`kWh_t`, accumulated as `Σ tare.w × dt` over the readings of the session: per channel in Independent mode, summed over
the channels in Shared mode). Session progress items carry `kwh_t` when a tare is set (`s.kwh_t`, `ss[].kwh_t`).

### 13.6 Server pages

Meter → Configuration → **Tune** (all meters; one block per channel on multi-channel meters): current zero / tare with date and user;
**[Check sensor]** for Zero and for Tare (progress "7 / 10 readings", then the numbers and OK / not stable / load still connected),
**[Set zero]** / **[Set tare]**, **[Clear]**. The *Tare* fields of the old Configure form (single-channel) are replaced; the
*Use tared values for limits* option stays in Configure. Display: the *Raw | Tared* switch and the tared columns exist for every
meter that has a tare on any channel.

### 13.7 Calibration (held) — design

**Goal:** correct the gain / non-linearity of one channel with a few known loads, from the server, erasable.
1. **Zero point** — the channel's zero (13.4), measured with no load.
2. **Points 1..3** — the user connects a **known load** and enters its true value from a reference meter, **A or W** (per point);
   the page starts a sensor check (13.1, ≥ 10 stable readings) and stores `(measured, true)`. Points must be increasing, at least 10 %
   apart; unstable checks are refused.
3. **Correction curve** — piecewise linear through `(0, 0)`, `(m1, t1)`, `(m2, t2)`, `(m3, t3)`; above the last point the last
   segment's gain continues. Current and power are corrected from their own points (A points correct current, W points power).
4. **Meter:** `cfg.ch[i].cal = {"a": [[m,t],…], "w": [[m,t],…]}` applied **after** zero and before tare / limits; stored with the config.
5. **Erase** = remove the points (the meter goes back to the default, uncorrected values); zero and tare are separate and stay.
6. **Run from the server** as a wizard (*Zero → known load 1 → 2 → 3 → Review → Apply*) with the measured vs true table and the
   resulting gain per segment (a gain outside 0.8–1.25 is refused as "wrong reference or wiring").
Calibration changes the values that sessions, kWh limits and costs are based on, so it is built after zero / tare have been proven
on a real meter.

## 14. Firmware update over the air (OTA) and USB flash — firmware / server 1.1.0

Status: **implemented** (server + both sketches); tested with the PC tests and the simulated meter, **not yet on real hardware**. The first OTA-capable firmware has to be
flashed over USB once; after that, updates go from the server. Transport for now: **HTTP(S) only** (the meter downloads the file).

### 14.1 What the administrator uploads
The zip made by the Arduino IDE (*Export compiled binary*, zipped by the user). Only **one file** is used, `<sketch>.ino.bin` (the
app image); the bootloader, partitions, `boot_app0` and `merged.bin` are only for USB flashing and are ignored by OTA.

On upload the server:
- finds the app image: `*.ino.bin` that is not `*.bootloader.bin`, `*.partitions.bin` or `*.merged.bin`; stores that file and its
  SHA-256, **and** the files for a USB flash (14.8: `flash_args`, `*.bootloader.bin`, `*.partitions.bin`, `boot_app0.bin`, `partitions.csv`; `merged.bin` only as a fallback) — not the `.elf` / `.map`; limits the zip and file sizes and rejects odd paths (zip-slip);
- reads the **firmware tag** inside the image. Every firmware contains a constant string `PMFWTAG|<type>|<version>|<hw>|` (placed
  in the code with `used` so the linker keeps it; the Arduino build does not carry the sketch name, so the tag is the reliable way).
  `<type>` is a free text id (`rbamp`, `pzem`, later `logger-pzem`, `smart-relay` ... the server has **no fixed list**: it shows
  whatever the tag says). No tag → refused ("not a PowerManager firmware");
- compares `partitions.csv` of the zip with the layout of the meters (it is also reported in the hello, 14.3): a different layout
  cannot be changed by OTA and is refused with "needs a USB flash";
- checks the size against the OTA slot.

### 14.2 Server data and pages
`Firmware` row: id, title (short), info (short), description, version (pre-filled from the tag; warn when edited to something
else), **type** (from the tag), size, sha256, file name, partitions hash, uploaded_at, uploaded_by.
- List: checkbox (bulk delete for now), title, info, **type**, version, size, by, [Flash] [Info] [Edit] [Delete]. Delete is refused
  while the firmware is being flashed. Edit changes the text fields; replacing the zip is optional.
- Add / Edit / Delete: admin only. Info and Flash: admin, and a manager for **their own meters** (managers cannot add firmware yet).
- Info (read only): title, info, description, version, type, size, updated date / time, updated by.
- Flash: *search and select a meter* → the meter's information (as *Get meter info*) next to the firmware information, side by
  side: type, version (running → new), hardware, flash size, partition layout, free OTA space, channels. A verdict on top:
  **blocked** (type differs from `hello.fw_type`, layout differs, too big, meter firmware without `caps.ota`: "needs one USB
  flash first", meter offline) or **warning** (downgrade, a session is running). The text says: keep the internet connection
  stable, unplug the appliances (the relays switch off), wait for the reboot. A check box confirms the warnings; **[Upload the
  Firmware to Meter]** is greyed out when the meter is offline or blocked. Progress is shown on the same page.

### 14.3 Meter
`hello` carries `fw_type` (`FW_TYPE`), `hw` (`HW_NAME`), `caps.ota` = `{"slot": <bytes of the spare app slot>, "layout": "<CRC-32 of the
partition table: type, subtype, offset, size of every entry, 8 hex digits>", "tag": "PMFWTAG|…"}` and `ota` = `{"state": "ok" | "trial" |
"rolled_back", "ver", "why"?, "tried"?}`. A firmware without `caps.ota` cannot be updated over the air (one USB flash first).

Command (server → meter): `{"t":"cmd","c":"ota","path":"/api/v1/ota/<token>","size":N,"sha256":"…","ver":"1.3.0","type":"rbamp-mc"}`. The meter
builds the address from the server it is paired with (scheme, host, port; so a reverse proxy works) and refuses with an ack error
`busy`, `wrong_type`, `too_big`, `bad_request`, `no_server` (not paired / mesh leaf), `no_wifi`. Otherwise it **ends its sessions
(reason `restart`), saves, switches the relays off**, and a task downloads the image (HTTP / HTTPS, streaming, `Update`) into the spare
slot while it computes the SHA-256 and scans the stream for the firmware tag. It checks size, SHA-256 and that the tag type equals
its own `FW_TYPE`, then switches the boot slot and restarts. Events: `ota_progress` {state: downloading | verifying | rebooting, pct,
ver} (downloading every 10 %), `ota_failed` {why: size, sha256, wrong_type, timeout, flash_write, http_<code>, net_<code> ...}, `ota_ok`
{ver} after the confirmation. A failed download changes nothing (the sessions stay ended). The token is valid about 10 minutes and for
3 downloads. The hardware profile command is refused (`busy`) while an update runs.

Rollback is done by the app (the prebuilt Arduino bootloader has no rollback support): before the restart the meter stores in NVS
(namespace `ota`) `prev` (slot it came from) and `try` (the new version); every boot of a `try` firmware counts (`boots`). The new
firmware is **confirmed** when the server answers its hello (`welcome`) within `OTA_CONFIRM_MS` (config.h, default 120 s); then
`try` is cleared and it sends `ota_ok`. When the time runs out, or the firmware boots more than 3 times unconfirmed (crash loop), it sets
the boot slot back to `prev`, notes `rb` / `rbver` and restarts; the old firmware reports `ota.state = "rolled_back"` in its next hello.
Settings and sessions (NVS) are kept.

### 14.4 Sessions
When the OTA starts the sessions of the meter are **ended** (the server sends the stop for every running session first;
end reason "restart"); nothing resumes by itself afterwards. The administrator is warned before (the check box).

### 14.5 Notifications: reboot pending
New notification type `reboot_pending` ("Meter restart pending (hardware change / firmware update)"), on by default for the users
who get meter status: sent when a restart is announced and when it is done (or rolled back / failed):
- hardware profile with `mode: reboot` (the 60 s delay of §12.4, event `hw_reboot_pending`): "Meter X restarts in 60 s: hardware change";
- OTA: "Meter X is updated to 1.3.0 and restarts" then "updated" / "rolled back to 1.2.0" / "failed: reason".
The AUX source *reboot* (§12) also stays active for the OTA restart.

### 14.6 Bulk update (planned, not now)
Select several meters of the **same type** and one firmware; the server sends them one after another (not in parallel; the next
starts when the previous is confirmed or failed) and shows a table of results. The per-meter flow above stays the building block.

### 14.8 USB flash from the browser (Web Serial, like esptool.spacehuhn.com)
The Flash page has two ways, chosen at the top: **Over the air** (14.2, the meter must be online) and **By USB (browser)**. The USB
way does **not** need the meter to be online, paired or even running: it writes the flash through the serial port of the browser.
It is also the way to flash the **first** OTA-capable firmware, to change the partition layout, and to recover a meter that does not boot,
without the Arduino IDE.

- **Browser:** needs Web Serial: Chrome, Edge, Opera on a computer (not Firefox, not Safari, not iOS). The page needs a **secure
  context**: `https://` or `http://localhost`. Both are supported: the normal setup is HTTPS from the reverse proxy (to be tested there)
  and `http://localhost` works on the server PC. On plain `http://` on the LAN the browser hides Web Serial, so the page shows that
  reason ("open the server over HTTPS") instead of a dead button. Without support the USB option is greyed out with the reason.
- **Tool:** Espressif's `esptool-js`, stored in `static/vendor` (works without internet, like the other vendor files).
- **Steps shown on the page:** connect the USB cable → *Connect* (browser port picker) → the page reads the chip (type, MAC, flash
  size) and checks it against the firmware (ESP32-S3, flash size at least the layout's) → options → *Flash* → progress bar and log →
  the board resets. If the board does not enter download mode by itself: hold BOOT, press and release RESET.
- **What is written** (offsets from the zip's `flash_args`; for an ESP32-S3: bootloader `0x0`, partitions `0x8000`, boot_app0
  `0xe000`, app `0x10000`); `merged.bin` at `0x0` only when `flash_args` is missing. Options:
  - **Update** (default): all files above, the settings area (NVS) is kept; pairing and Wi-Fi stay.
  - **Erase all flash first**: needed once when the layout changed (as for the 16 MB layout); the settings are lost and the meter is
    paired again. **Admin and manager** may use it, behind a typed confirmation like *Remove meter*: a red warning (settings, Wi-Fi and
    pairing are lost, the meter must be paired again) and the user must **type the device ID** of the meter to enable the button. The
    device ID is that of the meter chosen on the page (optional, offered for the manager's own meters); for a board that is not a
    paired meter yet, the text to type is the **chip MAC** shown after *Connect*.
- **Server side:** the browser downloads the files from an authenticated endpoint (admin, and a manager as for OTA); the server never
  talks to the board. At the end the page may report `usb_flash` (firmware id, chip MAC, who) to the server for the audit log; a flash
  that was cancelled halfway leaves the board in download mode, and can simply be flashed again.
- **The layout check of 14.1 does not apply** to USB (the partition table is written too); the type / hardware tag is still compared
  and a mismatch is a warning (a USB flash can put any firmware on a board).
- **Notifications:** none (nobody is told about a USB flash, there is no meter connection); the audit log entry is the record.

### 14.7 Open points
The USB way (14.8) needs HTTPS (reverse proxy) or `localhost`: to be tested behind the reverse proxy. `MAX_CONTENT_LENGTH` is 80 MB (a reverse proxy must allow uploads of that size, e.g. nginx `client_max_body_size`); the files live in the instance folder (`instance/firmware/<id>/`); a signature on the image (more than SHA-256) is a later option.

## 15. Disabled channel (multi-channel meters) — firmware / server 1.1.0

An admin / manager can **disable a channel** (Configuration → Channels → *Disable*). This is not the same as a disabled meter (the meter keeps running, its other channels are not affected).
- Config: `cfg.ch[i].dis` (bool, default false).
- Meter: when a channel becomes disabled its relay goes off and `wantOn` is cleared (it does not come back by itself when the channel is enabled again); in **independent** mode its session ends at once (`session_end`, reason `ch_disabled`); in **shared** mode the one session goes on for the other channels and the disabled channel is simply frozen (relay off, no start). While disabled: relay ON is refused (`ch_disabled`), a `renew` for that channel does nothing, auto renew skips it, and `state.relay[i].block = "ch_disabled"`. The channel is still measured.
- Server: `commands.issue` refuses `relay` ON (any mode) and `renew` with a channel (independent mode) for a disabled channel with the error `ch_disabled`; every user who can see the meter gets `channels[i].disabled` and the Info page shows a grey card with the badge **Disabled** and no buttons.
