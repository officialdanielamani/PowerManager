# Server Data Model

SQLite (WAL mode) through SQLAlchemy. All `*_at` / `ts` columns are **UTC epoch seconds** (integers). Converting to the display timezone happens only in the UI and CSV export.

## Tables

### `user`
| Column | Type | Notes |
|--------|------|-------|
| id | int PK | |
| username | str(32) unique | stored **lower-case** (case-insensitive login) |
| password_hash | str | Werkzeug scrypt/pbkdf2 |
| role | enum `admin` / `viewer` | exactly one `admin` |
| email | str(254) | |
| phone | str(16) | E.164, e.g. `+60123456789` |
| created_at, last_login_at | int | |
| is_enabled | bool | admin can disable viewers (also logs them out) |
| view_all | bool | viewer sees every meter |
| failed_logins, locked_until | int | viewer login lockout; `locked_until = -1` = until admin unlocks |
| telegram_chat_id | str | this user's Telegram chat (bot token is global) |
| can_csv, show_price | bool | viewer permissions (default off; admin always has both) |
| notif_autodel, notif_autodel_days | bool, int | delete own notifications older than N days (default off, 30) |

### `meter_viewer`
`device_fk, user_fk` (PK together). Which view-only users may see which meter. Server-only; never sent to meters.

### `setting` (key/value)
`server_address`, `server_port`, `server_tls`, `timezone` (IANA name, e.g. `Asia/Kuala_Lumpur`), `raw_retention_days` (default 90), `telegram_enabled`, `telegram_bot_token`, `login_max_attempts` (5), `login_lock_minutes` (15), `currency` (e.g. MYR), `price_per_kwh` (e.g. 0.21; empty = no cost shown).

### `pairing_code`
| Column | Notes |
|--------|-------|
| code (PK) | 8 chars |
| created_at, expires_at | 10 min lifetime |
| used_at, device_fk | set when paired |
| created_by | user id |

### `device`
| Column | Notes |
|--------|-------|
| id PK | |
| device_id | unique, from hardware (e.g. `MTR23H4R`), cannot be changed |
| name (≤40), info (≤200) | Nick Name, Info |
| secret_hash | |
| fw, hw, proto | from `hello` |
| caps (JSON) | sensor type, channels, relays, inputs, rtc |
| paired_at, last_seen_at | |
| online | bool (in-memory truth, persisted for display) |
| cfg (JSON), cfg_ver_wanted, cfg_ver_applied | see protocol §3 |
| last_state (JSON) | latest `state` message |
| last_seq | highest stored seq (current pairing generation) |
| pair_gen | pairing generation, +1 on every re-pair. `measurement`, `event` and `session` rows carry `gen`, so a factory-reset meter whose `seq` restarts at 1 does not collide with kept history. |
| removed_at | unsynced or removed (meter factory-reset) |
| purged | removed with all data (tombstone, hidden everywhere) |
| disabled_at | disabled: paired, read-only, relay forced off |
| resync_pending | an unsynced meter paired again: admin chooses continue / new |
| data_autodel(_days), events_autodel(_days) | per-meter clean-up (default off, 120 days) |
| last_info, last_info_at | last answer to Get meter info |
| tare_a, tare_w | own-consumption offsets (A, W) for tared views; raw data is never changed |

### `channel`
| Column | Notes |
|--------|-------|
| device_fk, idx | PK together |
| label | default `CH1` |
| kwh_max, amp_max | mirrored from cfg for querying |

### `measurement` (raw)
| Column | Notes |
|--------|-------|
| device_fk, ch, seq | **unique** together → resent data is ignored |
| ts | indexed with device_fk |
| v, a, w, pf, hz, e | REAL, nullable |
| time_estimated | bool |

### `rollup_dirty`
`device_fk, ch, period, bucket_start` — buckets that got new raw data (including late, buffered data) and need their roll-up rebuilt. Processed every 5 min.

### `rollup` (hourly and daily)
| Column | Notes |
|--------|-------|
| device_fk, ch, period (`h`/`d`), bucket_start | PK together. Buckets are UTC hours / UTC days. |
| n | sample count |
| v_min/v_max/v_avg, a_min/…, w_min/…, pf_avg, hz_avg | |
| kwh | energy used in the bucket (from the `e` difference, handling PZEM resets/rollovers) |

### `session`
| Column | Notes |
|--------|-------|
| device_fk, sid | sid comes from the device |
| started_at, ended_at | |
| kwh_used, kwh_max | |
| end_reason | `quota` / `expired` / `manual` / null (active) |
| auto | started by auto-renew |
| price_per_kwh, currency | electricity price **locked in when the session starts** (Settings → Server). Cost = kwh_used × price. Changing the price affects new sessions only. |

### `event`
`id, device_fk, seq, ts, type, ch, data (JSON), notified_at`. Includes server-made events: `disconnected`, `reconnected`, `config_applied`, `config_rejected`, `server_started`.

### `incident` (Historical Events)
Pairs a **start** event with its **end** event so the UI can show "from → to, how long".

| Column | Notes |
|--------|-------|
| id PK | |
| device_fk, ch | ch null for device-wide incidents |
| type | see table below |
| started_at | UTC |
| ended_at | UTC, **null = ongoing** |
| duration_s | filled when closed |
| start_event_fk, end_event_fk | links to `event` |
| detail (JSON) | e.g. trip current, who reset the trip, close source |
| time_estimated | bool, if a timestamp came from a device without valid time |

| `type` | Starts on | Ends on | Shown as |
|--------|-----------|---------|----------|
| `power` | `ac_lost` | `ac_restored` | Power disconnected → connected again |
| `offline` | server: WS closed / ping timeout | server: device reconnects (`hello`) | Offline → connected again |
| `trip` | `overcurrent` (detail: A measured, A limit) | `trip_reset` (detail: by user, server/local) | Power trip → reset |
| `tamper` | `tamper_open` | `tamper_close` | Tamper triggered → resolved |
| `voltage` | `voltage_out` | `voltage_ok` | Voltage out of the operating range → back in range |
| `sensor` | `sensor_error` | `sensor_ok` | No valid reading for the sensor timeout → readings back |

Rules:
- At most **one open incident per (device, ch, type)**. A repeated start event while one is open is ignored; an end event with no open incident is stored as an event only.
- **Offline times** use the server's clock. If the device sent `server_lost` / `server_back`, those times are stored in `detail` too (useful when the server itself was down).
- **Server restart:** on startup the server writes `server_started`. For each device that was online when the server stopped, it opens an `offline` incident starting at `last_seen_at` with `detail.cause = "server_down"`, closed when the device reconnects.
- **Missed end event:** if `hello.state` says the condition is gone (e.g. `tamper:false`) but the incident is still open, it is closed at the `hello` time with `detail.closed_by = "state_sync"`.
- Incidents are **kept forever** (they are small) and are deleted only with "Remove Device → also delete history".

### `notification_rule`
| Column | Notes |
|--------|-------|
| device_fk, user_fk, type | PK together: **per meter and per user** |
| enabled | bool |
| param | e.g. minutes for `disconnected` |

Types: `kwh_exceeded`, `voltage_range`, `sensor_error`, `session_renew`, `max_amp`, `panel_open`, `no_voltage`, `disconnected` (param = minutes), `reconnected`. Rules of a user who loses access to a meter are deleted.

### `notification`
`id, device_fk, user_fk, type, ts, message, read`. One row per receiving user, so each user has their own list and unread count. A **cooldown** per (device, user, type) — 10 min, only for `max_amp`, `panel_open`, `no_voltage` — stops repeated spam.

### Schema upgrades
`app/migrations.py` upgrades an existing database in place at start-up (v0.1 → v0.2: new user columns, `notification.user_fk`, per-user `notification_rule`; old rules and Telegram chat ID are given to the admin).

### `command`
`id, device_fk, c, params (JSON), created_by, created_at, sent_at, acked_at, ok, err`. Queued while the device is offline (see protocol §2.3).

## History queries and export

| Range picked | Source |
|--------------|--------|
| ≤ 48 h, or custom ≤ 48 h | raw `measurement` |
| ≤ 60 days | hourly `rollup` |
| longer / all time | daily `rollup` |

The stats panel shows min / max / average of V, A, W and total kWh for the picked range (hour, day, week, month, all time, custom).

CSV export covers **exactly the range selected** in the history view (1 week shown → 1 week exported), for admin and viewers. It uses raw data where it still exists (within the retention period), otherwise roll-ups (with min/max/avg columns). Timestamps are in **both** UTC and the display timezone. File name: `<device_id>_<from>_<to>.csv`.

## Removing a device

`device.removed_at` is set and `secret_hash` is cleared. If "also delete history" is ticked, its `measurement`, `rollup`, `session`, `event`, `command` and `notification_rule` rows are deleted too. The `device_id` row stays (with `removed_at`) so the device gets close code `4403` if it reconnects. If the same hardware is paired again later, the row is reused (`removed_at` cleared) and, if history was kept, the old history shows again.

## Retention and size

- Raw data is kept `raw_retention_days` (default 90); hourly roll-ups 2 years; daily roll-ups forever.
- Roll-ups are built by a job every 5 min (hourly) and hourly (daily), so they are already there before raw data is deleted.
- Rough size: 1 device at 10 s ≈ 8,640 rows/day ≈ 0.8 M rows per 90 days ≈ 80 MB. Fine for SQLite.

## Clean-up job (daily, 03:41 UTC)

- Per user (if enabled): notifications older than `notif_autodel_days`.
- Per meter (if enabled): `data_autodel_days` → finished sessions, raw measurements and roll-ups older than that; `events_autodel_days` → finished incidents and events older than that.
- Admins can also select and delete historical events (incidents) by hand; every user can select/delete their own notifications.

## No power + Offline

When an *Offline* and a *No power* incident are both ongoing and started within 60 s of each other, the meter itself lost power (its UPS ran out): the *No power* incident is closed (`closed_by: offline`) and the *Offline* incident gets `power_lost: true`.

## Tare

The offsets are part of the meter config (`tare_a`, `tare_w`, `tare_limits`), so the meter can use them for its limits (firmware 0.3+). Raw readings are stored unchanged. The meter reports its own tared session kWh (`kwh_t`), which the server shows; elsewhere tared values are computed for display/export:
`A' = max(0, A − tare_a)`, `W' = max(0, W − tare_w)`, `kWh' = max(0, kWh − tare_w × hours)` (hours = the session's duration, or the time covered by the history range/bucket). CSV exports of raw data get extra `A_tared` / `W_tared` columns when a tare is set.
