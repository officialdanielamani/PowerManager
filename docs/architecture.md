# Architecture

## 1. Overview

```
 ┌──────────────┐  WSS (runtime)        ┌───────────────────┐        ┌─────────────────────────┐
 │ ESP32 meter  │ ───────────────────▶ │ Cloudflare Tunnel │ ─────▶ │ Docker: PowerManager    │
 │ PZEM + SSR   │  HTTPS (pairing)      │ (TLS terminates)  │  HTTP  │ Flask + WS + Scheduler  │
 │ tamper + LED │                       └───────────────────┘        │ SQLite (volume)         │
 └──────┬───────┘                                                     └───────────┬─────────────┘
        │ local Wi-Fi AP / LAN HTTP                                               │ HTTPS
   ┌────┴─────┐                                                             ┌─────┴─────┐
   │ Phone /  │  setup, local dashboard                                     │ Admin /   │
   │ laptop   │                                                             │ viewers   │
   └──────────┘                                                             └───────────┘
```

## 2. Key decisions

| # | Decision | Why |
|---|----------|-----|
| D1 | **The meter enforces all limits locally** (over-current, kWh quota, session expiry, offline relay-off). The server owns the config and pushes it. | Must keep working when the server or Wi-Fi is down. |
| D2 | **Versioned config.** The server stores the wanted `config_version`; the device replies with `config_ack{version}`. The UI shows *Synced* or *Pending sync*. | "Saved and confirmed by the device" needs a clear, checkable meaning. |
| D3 | **One-time pairing code** (10 min, single use) in the QR. When pairing succeeds the server gives that device its **own secret**. | A leaked QR stops working on its own; one device can be removed without affecting the others. |
| D4 | **Pairing over HTTPS POST, runtime over WebSocket** on the same host/port as the web UI (`/ws/device`). | Works through Cloudflare Tunnel on port 443; there is no separate WS port to open. |
| D5 | **All timestamps are stored and sent in UTC (epoch seconds).** The display timezone is a server setting. | The user asked for UTC in the DB, local time on screen. |
| D6 | **Device → Channel → Measurement model** from day one, plus device *capabilities* sent at pairing. | Future multi-channel meters and other sensor types need no rewrite. |
| D7 | **Notifications are worked out on the server** from device events/state, through a channel layer (in-app first, Telegram later). | The device does not need to know about notification settings. |
| D8 | **The device's PZEM energy register is the source of truth for session kWh.** The device stores the register value when the session starts. | The PZEM keeps its energy count without power, which avoids flash wear on the ESP32. |
| D9 | **Separate "report interval" from "control loop".** Readings and limit checks run every ~1 s; reports go at 10 s–1 h; WS ping every 30 s. | Safety does not depend on the report rate, and offline detection still works with a 1 h interval. |

## 3. Server stack

| Concern | Choice |
|---------|--------|
| Web framework | Flask 3, Jinja templates, Bootstrap (served locally, like ElectroManager) |
| Auth | Flask-Login, Flask-WTF (CSRF), Flask-Limiter (login rate limit), Werkzeug password hashing |
| DB | SQLAlchemy + SQLite in WAL mode, file in `instance/` (Docker volume) |
| WebSocket | Flask-Sock (raw WebSocket, easy for the ESP32 `WebSockets` library) |
| Background jobs | APScheduler: offline timers, roll-ups, retention cleanup, pairing-code expiry |
| Charts | Chart.js (served locally) |
| QR | `qrcode` (server-side PNG/SVG) |
| Process | gunicorn, **1 worker** with threads (live WS connections are kept in that process's memory) |

### Docker layout

```
server/
  Dockerfile            python:3.11-slim (BASE_IMAGE build arg), pip install, front-end libs fetched at build
  docker-compose.yml    port 5000, volume ./instance:/app/instance (DB + secret key)
  .env.example          SECRET_KEY, SETUP_TOKEN, GUNICORN_THREADS
  app/                  Flask app (routes/, services/, templates/, static/)
  tools/sim_meter.py    simulated meter for testing without hardware
  tests/                pytest
```

Run it: download the zip or clone → `cd server` → `docker compose up -d --build`. See `server/README.md`.

### First run

1. No admin in the DB → every request redirects to `/setup`.
2. The setup page asks for a **setup token**. If `SETUP_TOKEN` is not set in the env, a random one is generated at each start and printed in the container log (`docker logs powermanager`). This stops a stranger on the internet from creating the admin before you do. `SETUP_TOKEN=off` disables the check (LAN only).
3. After the admin is created, `/setup` is permanently disabled.

## 4. Cloudflare Tunnel notes

- Tunnel ingress → `http://powermanager:5000` (or `http://localhost:5000`).
- Cloudflare supports WebSockets through tunnels. Idle WS connections are closed after ~100 s, so the device sends a `ping` message **every 30 s**.
- The device connects with `wss://<host>/ws/device` and validates the TLS certificate against an embedded set of root CAs (Cloudflare edge certificates chain to well-known public roots).
- If **Cloudflare Access** (Zero Trust login) protects the site, add a bypass for `/ws/device` and `/api/v1/pair`, otherwise the meter gets a login page. Also make sure Bot Fight Mode / WAF challenges do not apply to those paths.
- Flask runs behind `ProxyFix` so it sees the real `https` scheme and host.

### Public server address setting

**Settings → Server** holds the address that goes into the pairing QR:

| Field | Example | Notes |
|-------|---------|-------|
| Address | `power.example.com` or `192.168.1.10` | Required for pairing; defaults to the host the admin is browsing from |
| Port | empty / `5000` | Empty = default for the scheme (443 / 80) |
| Secure (TLS) | on | on → `https` + `wss`; off → `http` + `ws` (LAN use) |

## 5. Session model

A **session** is a period with a kWh quota. It ends by one of:

| End reason | Trigger | Relay after |
|------------|---------|-------------|
| `quota` | kWh used ≥ quota | OFF until next renew |
| `expired` | Duration elapsed, or daily reset time reached | Auto-renew on → new session starts at once; off → **OFF until [Renew Now]** |
| `manual` | [Renew Now] pressed | New session starts at once |

Reset schedule modes:

- **Duration**: days/hours/minutes counted from session start.
- **Daily**: a fixed local time (e.g. 01:00). The server converts it to a **UTC minute-of-day** using the timezone setting and sends that to the device, so the device only works in UTC. If the timezone setting changes, the server pushes a new config.

## 6. Roles

| Role | Can |
|------|-----|
| Admin (exactly 1) | Everything: pair/configure/remove meters, relay, renew, settings, manage view-only accounts, link meters to users |
| View-only (0..n, created by admin) | See **only the meters linked to them** (or all meters if *View all meters* is ticked): info, history, events, CSV export. Choose **their own** notifications per meter. Change own password / email / phone / Telegram chat ID. **Cannot** change anything about meters. |

- A meter can be linked to many view-only users; a user can be linked to many meters. Links are set from the meter (*Configuration → Link this meter to users*) or from the user (*Settings → Users → Manage*).
- Links live only on the server. They are **never sent to the meter**.
- Admin can create, delete, enable/disable (selected or all), unlock and reset the password of view-only accounts.
- Login protection: view-only accounts lock after *N* failed logins (default 5) for *M* minutes (default 15; 0 = until the admin unlocks). The admin account is never locked (otherwise anyone could lock you out); every login form is rate limited to 5 tries per minute per IP.

### Pages per meter

| Page | Who | Content |
|------|-----|---------|
| Info | admin + linked viewers | Live data, history, events, sessions, CSV |
| Notification Options | admin + linked viewers | **Per account**: each user ticks the notifications they want for this meter |
| Configuration | admin | Name, info, interval, limits, session, linked users, Renew / Set to Default / Remove |

Example: meter "Dryer A" — admin ticks *Panel open* + *No voltage*; the tenant ticks everything. Each gets only what they chose (in-app, plus Telegram if they set a chat ID).

## 7. Confirmed behaviour

- **B1 "Set to Default"** resets the meter's config (limits, interval, session) to defaults and pushes it. It does **not** unpair, rename, change linked users, change anyone's notification options or clear history.
- **B2** After an *offline relay-off*, when the device comes back online the relay goes back to the user's wanted state (if no other rule blocks it).
- **B3** Viewers may export CSV. The CSV contains **exactly the range shown** in the history view (e.g. view = 1 week → CSV = that week only).
- **B4 Disable / Unsync / Remove** (admin, with a confirmation dialog that explains each):
  - **Disable**: session ends, relay OFF, Relay ON refused (server and meter). The meter stays paired but read-only: no configuration changes, new readings/events are acknowledged but not stored, no Offline/No power incidents. Ongoing incidents are ended. Data stays. **Enable** undoes it, but does not start a session: press **Renew Now** when ready.
  - **Unsync**: session ends, the meter factory-resets (now, or with close code `4403` when it next connects). Ongoing incidents are ended. Data stays (listed under *Unsynced meters*). If the same hardware pairs again, the admin chooses **continue** (same meter, history continues) or **new meter** (the old history moves to an archived entry `<ID>~1`).
  - **Remove**: session ends, the meter factory-resets, and **all data is deleted** (the admin types the Device ID to confirm). A small tombstone row stays so an offline meter still gets `4403`; pairing that hardware again creates a fresh meter.
- **B5** Over-current trip is **latched**: relay OFF + red solid until [Reset Trip] on the server or on the device dashboard.

## 8. Capacity and security

### How many meters?

Each connected meter uses one server thread (default pool: 100) and **no database connection while idle** (one is taken only while a message is handled). Measured with the simulator:

| Test | Result |
|------|--------|
| 30 meters, each reporting every 10 s, for 90 s | 30/30 online, server worker **0.7% CPU, 85 MB RAM**, 0 errors |
| Dashboard / history API while 30 meters are connected | 20–140 ms |

So 30 meters is comfortable; ~90 is possible with the defaults (raise `GUNICORN_THREADS` above that). SQLite handles this write rate easily.

### Can someone send fake readings?

Not without that meter's secret:

- Every meter gets its **own 256-bit secret** at pairing. The server stores only a hash. The WebSocket is refused (`4401`) without the right `device_id` + secret.
- The secret travels only inside TLS (HTTPS/WSS through Cloudflare). The meter checks the server certificate against built-in root CAs, so it cannot be tricked into talking to a fake server.
- Pairing codes are single-use, expire after 10 minutes, and pairing is rate limited (10/min per IP).
- Readings outside physical ranges (e.g. > 400 V, PF > 1) are stored as "no value".
- A second connection with the same credentials replaces the first; both events are logged.

Remaining risks and how to reduce them:

| Risk | Mitigation |
|------|------------|
| Someone with **physical access** reads the secret from the meter's flash | Panel tamper switch + event; *Remove Device* revokes that one meter. Future: enable ESP32 flash + NVS encryption. |
| Plain `ws://` on an untrusted LAN exposes the secret | Use TLS (Cloudflare) — on the LAN only if you trust it |
| Bot / WAF challenges blocking meters | Bypass `/ws/device` and `/api/v1/pair` in Cloudflare Access/WAF only |
