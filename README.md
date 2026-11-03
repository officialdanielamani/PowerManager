# PowerManager

Self-hosted energy meter system: ESP32 meters measure and switch loads, and a Docker server manages them, applies session/kWh limits and keeps history.

> **Status:** foundation / design stage. There is no code yet. The documents in `docs/` are the contract both sides will be built against.

## Components

| Part | Tech | Folder |
|------|------|--------|
| Server | Python 3.11, Flask, SQLite, WebSocket, runs in Docker | `server/` |
| Meter firmware | ESP32-S3 Super Mini, Arduino IDE (Arduino-ESP32 core 3.x) | `firmware/` |
| Docs | Markdown | `docs/` |

## Documents

| Doc | Content |
|-----|---------|
| [docs/architecture.md](docs/architecture.md) | System overview, key decisions, deployment (Docker + Cloudflare Tunnel) |
| [docs/protocol.md](docs/protocol.md) | Pairing flow and WebSocket message contract between meter and server |
| [docs/data-model.md](docs/data-model.md) | Server database tables, retention, roll-ups |
| [docs/server-ui.md](docs/server-ui.md) | Server pages, fields, validation rules, roles |
| [docs/firmware.md](docs/firmware.md) | ESP32 behaviour: setup, tasks, enforcement, LED, buffering, pin map, libraries |

## Planned deployment (summary)

Same style as [ElectroManager](https://github.com/officialdanielamani/ElectroManager): download the repo (zip or `git clone`), then build and run with Docker Compose:

```bash
cd PowerManager/server
docker compose up -d --build
```

The server is exposed to the internet through a Cloudflare Tunnel (HTTPS/WSS). See `docs/architecture.md`.
