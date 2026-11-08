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
  "hw": "esp32s3-supermini",
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
| `info` | Reply to `get_info` | `ip`, `ssid`, `rssi`, `mac`, `heap`, `heap_min`, `psram`, `psram_free`, `fw`, `hw`, `hw_info` (config.h `HW_INFO`), `chip`, `flash`, `core`, `uptime`, `boot`, `outbox`, `time_src`, `stack` (free stack per task), `reset` (last reset reason, SAFE MODE), `clock` (meter clock, epoch; 0 = not set), `rtc` (DS3231 time; only with a DS3231, 0 = not readable), `sensor` / `kind` (AC/DC Power Sensor), `bat_v` + `bat_pct` when `BATTERY_ENABLE` is on, `temp_sensors` (serial number and value of each DS18B20) with `TEMP_ENABLE`; multi-channel: `clamps`, `sensor_boards`, `relay_board` |
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
| `quota` | Session kWh ≥ `kwh_max`. `action`: `end` → followed by `session_end` reason `quota` (the **server also closes the session record on this event**, in case `session_end` is late); `hold` → relay OFF, session stays open; `warn` → power stays on (sent once per session). `end` (bool) is kept for older servers |
| `session_end` | Session ended (`reason`: quota/expired/manual). Sent **once** per session: renewing a session that a limit already ended does not send another |
| `session_start` | New session (`sid`, `auto`: bool) |
| `tamper_open` / `tamper_close` | Panel switch. With Tamper Protection on, `tamper_open` carries `action` |
| `sensor_error` / `sensor_ok` | No valid reading for `sensor.timeout_s`: `{"kind": "no_reply"\|"bad", "after_s": 10, "action": "hold"}`; `sensor_ok` = first valid reading again |
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
    "ch": [ {"i":0, "kwh_max":2.0, "kwh_action":"end", "kwh_end":true, "amp_max":10.2} ]
  }
}
```

| Field | Range | Default | Notes |
|-------|-------|---------|-------|
| `report_s` | 10–3600 | 60 | Send buffered readings to the server every … s (events go right away) |
| `read_s` | 5–`report_s` | = `report_s` | Record a reading every … s. Example: `read_s` 10 + `report_s` 60 = 6 readings sent together once a minute. Missing (older server) = one reading per send |
| `offline_off_min` | 0–1440 | 0 | 0 = disabled |
| `sess.mode` | `daily` / `duration` / `none` | `none` | `none` = no time limit |
| `sess.daily_utc_min` | 0–1439 | – | Local reset time converted to UTC by the server. Example: 01:00 at UTC+08:00 → 17:00 UTC → `1020` |
| `sess.dur_min` | 1–525600 | – | Duration mode; UI enters days/hours/minutes |
| `sess.auto` | bool | false | |
| `ch[].kwh_max` | 0–100000 | 0 | 0 = unlimited |
| `ch[].kwh_action` | `warn` / `hold` / `end` | `hold` | What happens at `kwh_max`. **warn** = *Session run as usual* (`quota` event once + `sess.over`, power stays on). **hold** = *Off relay, but session continue* (relay OFF until the session ends at its time limit or is renewed). **end** = *End session* now (relay OFF until renewed). |
| `ch[].kwh_end` | bool | true | Only for firmware 0.1.x, which predates `kwh_action`: `false` = warn, otherwise end. New firmware uses `kwh_action` when present. |
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
