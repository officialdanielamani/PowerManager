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
| is_active | bool | admin can disable viewers |

### `setting` (key/value)
`server_address`, `server_port`, `server_tls`, `timezone` (IANA name, e.g. `Asia/Kuala_Lumpur`), `raw_retention_days` (default 90), `telegram_bot_token`, `telegram_chat_id` (future), `setup_done`.

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
| last_seq | highest stored seq |
| removed_at | soft delete |

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

### `rollup` (hourly and daily)
| Column | Notes |
|--------|-------|
| device_fk, ch, period (`h`/`d`), bucket_start | PK together |
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

### `event`
`id, device_fk, seq, ts, type, ch, data (JSON), notified_at`. Includes server-made events: `disconnected`, `reconnected`, `config_applied`, `config_rejected`.

### `notification_rule`
| Column | Notes |
|--------|-------|
| device_fk, type | PK together |
| enabled | bool |
| param | e.g. minutes for `disconnected` |

Types: `kwh_exceeded`, `session_renew`, `max_amp`, `panel_open`, `no_voltage`, `disconnected` (param = minutes), `reconnected`.

### `notification`
`id, event_fk, channel (inapp/telegram), sent_at, status, error`. A **cooldown** per (device, type) — default 10 min — stops repeated events from spamming.

### `command`
`id, device_fk, c, params (JSON), created_by, created_at, sent_at, acked_at, ok, err`. Queued while the device is offline (see protocol §2.3).

## History queries and export

| Range picked | Source |
|--------------|--------|
| ≤ 48 h, or custom ≤ 48 h | raw `measurement` |
| ≤ 60 days | hourly `rollup` |
| longer / all time | daily `rollup` |

The stats panel shows min / max / average of V, A, W and total kWh for the picked range (hour, day, week, month, all time, custom).

CSV export uses raw data where it still exists (within the retention period), otherwise roll-ups. Timestamps are in **both** UTC and the display timezone.

## Retention and size

- Raw data is kept `raw_retention_days` (default 90); hourly roll-ups 2 years; daily roll-ups forever.
- Roll-ups are built by a job every 5 min (hourly) and hourly (daily), so they are already there before raw data is deleted.
- Rough size: 1 device at 10 s ≈ 8,640 rows/day ≈ 0.8 M rows per 90 days ≈ 80 MB. Fine for SQLite.
