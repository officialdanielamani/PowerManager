# PowerManager

Self-hosted energy meter system: ESP32 meters measure and switch loads, and a Docker server manages them, applies session/kWh limits and keeps history.

> **Status:** server v0.1 and meter firmware v0.1 are written. The server is tested end-to-end with a simulated meter; the firmware compiles cleanly and is waiting for hardware testing. The documents in `docs/` are the contract both sides are built against.

## Components

| Part | Tech | Folder |
|------|------|--------|
| Server | Python 3.11, Flask, SQLite, WebSocket, runs in Docker — see [server/README.md](server/README.md) | `server/` |
| Meter firmware | ESP32-S3 Super Mini, Arduino IDE (Arduino-ESP32 core 3.x). AC meter (PZEM-004T) and DC meter (PZEM-017) from one source — see [firmware/README.md](firmware/README.md) | `firmware/PowerMeter/` |
| Multi-channel AC meter firmware | ESP32-S3 + one or more rbAmp BASIC modules (1–16 SCT-013 channels) + relays (GPIO / PCF8574 / XL9535) — see [firmware/PowerMeterRBAmp/README.md](firmware/PowerMeterRBAmp/README.md) | `firmware/PowerMeterRBAmp/` |
| Docs | Markdown | `docs/` |

## Documents

| Doc | Content |
|-----|---------|
| [docs/architecture.md](docs/architecture.md) | System overview, key decisions, deployment (Docker + Cloudflare Tunnel) |
| [docs/protocol.md](docs/protocol.md) | Pairing flow and WebSocket message contract between meter and server |
| [docs/data-model.md](docs/data-model.md) | Server database tables, retention, roll-ups |
| [docs/server-ui.md](docs/server-ui.md) | Server pages, fields, validation rules, roles |
| [docs/mesh.md](docs/mesh.md) | Mesh over ESP-NOW: roles, security, radio details, test plan |
| [docs/firmware.md](docs/firmware.md) | ESP32 behaviour: setup, tasks, enforcement, LED, buffering, pin map, libraries |
| [docs/multichannel.md](docs/multichannel.md) | Multi-channel AC meter: hardware, Shared / Independent modes, protocol additions |

## Planned deployment (summary)

Same style as [ElectroManager](https://github.com/officialdanielamani/ElectroManager): download the repo (zip or `git clone`), then build and run with Docker Compose:

```bash
cd PowerManager/server
docker compose up -d --build
docker logs powermanager      # shows the one-time setup token
```

The server is exposed to the internet through a Cloudflare Tunnel (HTTPS/WSS). See `docs/architecture.md`.
