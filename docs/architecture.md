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
  Dockerfile            python:3.11-slim, pip install, gunicorn
  docker-compose.yml    port 5000, volume ./instance:/app/instance, TZ not needed (UTC internally)
  .env.example          SECRET_KEY, SETUP_TOKEN (optional)
  app/ ...
```

Run it: download the zip or clone → `cd server` → `docker compose up -d --build`.

### First run

1. No admin in the DB → every request redirects to `/setup`.
2. If `SETUP_TOKEN` is set in the env, the setup page asks for it. The token is printed in the container log. This stops a stranger on the internet from creating the admin before you do.
3. After the admin is created, `/setup` is permanently disabled.

## 4. Cloudflare Tunnel notes

- Tunnel ingress → `http://powermanager:5000` (or `http://localhost:5000`).
- Cloudflare supports WebSockets through tunnels. Idle WS connections are closed after ~100 s, so the device **pings every 30 s**.
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
| Admin (exactly 1) | Everything: pair/configure/remove meters, relay, renew, settings, manage viewers |
| Viewer (0..n, created by admin) | View dashboard, meter info, history; export CSV; change own password. **Cannot** change anything about meters. |

## 7. Confirmed behaviour

- **B1 "Set to Default"** resets the meter's config (limits, interval, notifications) to factory defaults and pushes it. It does **not** unpair, rename or clear history.
- **B2** After an *offline relay-off*, when the device comes back online the relay goes back to the user's wanted state (if no other rule blocks it).
- **B3** Viewers may export CSV. The CSV contains **exactly the range shown** in the history view (e.g. view = 1 week → CSV = that week only).
- **B4 "Remove Device"**:
  - Server: the device is marked removed and its secret is revoked. History is **kept** unless the admin ticks "also delete history".
  - Device: **full factory reset** — wipes local admin, Wi-Fi, pairing, config, session and buffer, then reboots into first-boot AP mode (create admin account). The AP password is kept, so the label stays valid.
  - If the device is offline at removal time, the reset happens on its next connect (protocol §2: close code `4403`).
- **B5** Over-current trip is **latched**: relay OFF + red solid until [Reset Trip] on the server or on the device dashboard.
