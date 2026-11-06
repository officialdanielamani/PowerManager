# Multi-channel AC meter (rbAmp)

A separate firmware (`firmware/PowerMeterMC/`) for an ESP32-S3 Super Mini with an **rbAmp BASIC** module
(SCT-013 clamps), a DS3231, a tamper limit switch and an **I²C relay board** (PCF8574 + SSRs, or an
XL9535 relay board with 1/2/4/8 relays). One voltage input, 1–7 current channels, one relay per channel.

The server treats it as an AC meter with `caps.mc = true` and `caps.channels = N`.

## Hardware summary

| Part | Bus / pin | Notes |
|---|---|---|
| rbAmp BASIC (SCT-UI1/2/3/5/7, SCT-I1/2/3) | I²C `0x50` | variant read from `HW_VARIANT` (`0x55`); 5 V supply, isolated from mains; answers during a power cut (U ≈ 0) |
| rbAmp DRDY | GPIO (`PIN_DRDY`) | open-drain, ~10 µs low pulse every 100 ms window; pull-up 10 k (or the internal one) |
| DS3231 | I²C `0x68` | (its EEPROM at `0x57`) |
| PCF8574 / PCF8574A | I²C `0x20–0x27` / `0x38–0x3F` | one bit per relay |
| XL9535 relay board | I²C `0x20–0x27` | TCA9535-compatible: port 0 = relays 1–8 |
| Tamper switch | GPIO 4 | one for the enclosure |
| Battery sense | GPIO 1 | as in the PZEM firmware |

All I²C devices share one bus at **50 kHz** (rbAmp recommendation for ESP32 hosts), guarded by one mutex.

**Energy:** the rbAmp BASIC does not keep energy. The meter closes a measurement period every second
(`CMD_LATCH_PERIOD`), reads each channel's average power over that period and adds `avg_W × dt` to its
own per-channel kWh counter (`e`), which is saved in NVS (every 5 min and on every session change).
The first period after start-up or after a module restart is discarded (rbAmp primer rule).

## Modes (server configuration)

| | **Shared** (default) | **Independent** |
|---|---|---|
| Session | one for the whole meter (`sess`) | one per channel (`sessions[]`) |
| kWh limit | total of all channels (`kwh_max`) | per channel (`ch[i].kwh_max`) |
| Time limit / auto renew | one (`sess`) | per channel (`ch[i].sess`) |
| Limit reached | every channel's relay follows `kwh_action` | that channel's relay only |
| Renew / End | whole meter | per channel, plus **Renew all** / **End all** |
| Relay ON/OFF | per channel | per channel |
| Max current (trip) | per channel | per channel |
| Voltage range, tamper, sensor timeout | meter-wide: the action applies to all channels / sessions | same |

Changing the mode ends the running session(s); press Renew to start.

Session numbers (`sid`) come from one counter on the meter, so they are unique across channels.

## Protocol additions (proto 1)

### caps (pair + hello)

```json
{"kind":"ac","mc":true,"sensor":"rbamp","variant":"UI2","channels":2,"relays":2,
 "voltage":true,"drdy":true,"amp_rating":30,"ratings":[30,30]}
```

`voltage: false` (SCT-I* variants): no voltage → no power / energy (only current); no "No AC" detection.

### cfg (server → meter)

```json
{"ver":7,"cfg":{
  "report_s":60,"read_s":10,"drdy":false,"offline_off_min":0,
  "mode":"shared",
  "sess":{"mode":"none","auto":false},           // shared mode session
  "kwh_max":10.0,"kwh_action":"hold",             // shared mode: total of all channels
  "volt":{"min":0,"max":0,"action":"hold"},
  "tamper":{"protect":false,"action":"hold"},
  "sensor":{"timeout_s":10,"action":"hold"},
  "ch":[{"i":0,"name":"Kitchen","amp_max":16,
         "kwh_max":8.0,"kwh_action":"hold",       // independent mode
         "sess":{"mode":"daily","daily_utc_min":0,"auto":true}}, …]}}
```

`drdy: true` = read the module on every DRDY pulse (100 ms): limits are checked on every window. Without it
the module is read once a second. Readings are recorded raw (no averaging) every `read_s`
(1–3600 s with `drdy`, 5–3600 s without) and sent every `report_s`.

Why not record every 100 ms window: that is ~2 KB/s into the meter's flash buffer (it would wear out the
flash within months) and ~1.7 million database rows per meter per day. A short live-capture mode streaming
from RAM could be added later if 100 ms data is needed.

### state

```json
{"mode":"shared",
 "relay":[{"i":0,"on":true,"want":true,"block":null,"tripped":false}, …],
 "tripped":false,"tamper":false,"ac":true,
 "sess":{"sid":12,"start":…,"kwh":1.234,"max":10,"status":"active","ended":false,"next_reset":…},
 "sessions":[{"ch":0,"sid":13,…},{"ch":1,"sid":14,…}]}      // independent mode only
```

In shared mode `sess` is the meter's session; in independent mode `sess` is omitted and `sessions` has one
entry per channel (same fields as `sess`, plus `ch`).

### Telemetry item

`ch[i].e` is the meter's own kWh counter of channel i. Session progress: `s` (shared) or
`ss:[{"ch":0,"sid":13,"kwh":0.5}, …]` (independent).

### Events (`ch` added where it applies)

`overcurrent {ch,val,limit}`, `trip_reset {ch}`, `relay {ch,on,src}`, `quota {ch?,kwh,action}`,
`session_start {sid,ch?,auto,max}`, `session_end {sid,ch?,reason,kwh}`. `ch` absent = shared session / whole meter.
New session end reason `stopped` (End / End all).

### Commands

| `c` | params | |
|---|---|---|
| `relay` | `{ch, on}` | one channel |
| `renew` | `{ch?}` | shared: the session; independent: that channel, or all channels without `ch` |
| `end` | `{ch?}` | end the session(s) now (relay off until renewed) |
| `reset_trip` | `{ch?}` | one channel, or all |

## Changing the hardware on a meter

The Device ID comes from the ESP32, so flashing another firmware type on the same board (PZEM AC, PZEM DC,
rbAmp with N channels) keeps the same meter on the server. Both sides handle this automatically:

- **Meter:** each firmware stores its type (`FW_TYPE`). A different type on first boot clears the old
  sessions, kWh counters, relay / trip state and config. Wi-Fi, the admin login, the pairing, "disabled"
  and the sequence numbers stay, so no re-setup or re-pairing is needed.
- **Server:** every `hello` carries the type (`caps.kind`, `caps.mc`, `caps.channels`). If it differs from
  the last one, the meter's history (readings, sessions, events, notifications) moves to an archived meter
  **`<ID>~n`** named "<name> [old type]" under *Unsynced / archived meters*, kept for reference (viewers of
  the meter can still open it). The live meter keeps its name, info, linked users and notification options,
  gets the default config of the new type and a new pairing generation, and a **Meter status** notification
  says what changed. A multi-channel meter whose module did not answer yet (channel count unknown) never
  counts as a change.

Example: PZEM AC → rbAmp 5 ch → PZEM DC → rbAmp 2 ch → PZEM AC leaves one live meter and four archives.

## AUX relays

Relays on the board beyond the measuring channels (e.g. 2 channels on a 4-relay board: relays 3 and 4)
are **AUX relays**: manual ON / OFF only (server Info page → Channels, or the meter's own page). They are
not measured, not recorded and not tied to limits, sessions or protections. They keep their ON / OFF over
a reboot; a disabled meter switches them off.

Protocol: `caps.aux` = number of AUX relays; state `aux: [{"i":0,"relay":3,"on":true}, …]`; command
`aux {i, on}`; event `aux_relay {i, relay, on, src}`; config `aux: [{"i":0,"name":"Pump"}, …]` (names).

## No session after pairing

Since firmware 0.6.4 / MC 0.7.2 a newly paired meter does not start a session on its own: it shows
**No session** and the relays stay off (`block: no_session`) until the admin presses **Renew**.

## Data modes (Configuration → General → Data)

- **Normal mode:** read every X s (1 s – 1 h) and send every Y s (10 s – 1 h, read ≤ send).
- **DRDY:** no read interval. The rbAmp pulses DRDY once per measurement window (~100 ms on current module
  firmware, ~200 ms before v1.2) and **every window is recorded raw** (≈ 10 readings per second). Send every
  1–90 s. The meter packs each second's windows into one record (`"fr": [[ms after ts, V, A1, W1, PF1, A2, …], …]`,
  plus `"e"` = kWh counters and `"hz"`); the server stores one reading per window (`ms` = milliseconds).
  About 860,000 rows per channel per day: use it for detailed measurements.

Limits and protections are checked on every reading: every window in DRDY mode, every second in Normal mode,
whatever the send interval.

Readings wait in the meter's RAM until the server acknowledges them and only go to the flash buffer when they
cannot be sent (RAM full, unsent for too long, or before an event), so fast data does not wear out the flash.

## CSV

Multi-channel meters export **one row per reading with every channel side by side**: `ms, V, Hz`, then for each
channel `<name> A`, `<name> W`, `<name> PF`, `<name> kWh total` (names as in Configuration, e.g. "CH1 Kitchen").
Session CSVs work the same. Single-channel meters keep their format; the channel column starts at 1.
