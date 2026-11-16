# PowerManager

Self-hosted energy meter system: ESP32-S3 meters measure and switch loads, and a Docker server manages them,
applies session / kWh limits, keeps history and sends notifications.

> **Status (server 1.0.0, firmware 1.0.0):** the server is covered by automated tests and tested end-to-end with
> simulated meters; both firmwares compile and their PC tests pass. Several newer features have not been tried on
> real hardware yet. See [docs/handover.md](docs/handover.md) for exactly what is and is not verified.

## Components

| Part | Tech | Folder |
|------|------|--------|
| Server | Python 3.11, Flask, SQLite, WebSocket + optional MQTT bridge, runs in Docker | [`server/`](server/README.md) |
| Meter firmware, 1 channel | ESP32-S3 + PZEM-004T (AC) or PZEM-017 (DC), one source | [`firmware/PowerMeterPZEM/`](firmware/README.md) |
| Meter firmware, multi-channel AC | ESP32-S3 + rbAmp BASIC modules (1–16 SCT-013 channels) | [`firmware/PowerMeterRBAmp/`](firmware/PowerMeterRBAmp/README.md) |
| Docs | Markdown | [`docs/`](docs/) |

Both firmwares share: Wi-Fi setup page, pairing, main + backup Wi-Fi, WebSocket and / or MQTT link, ESP-NOW mesh,
offline buffer, relay outputs (GPIO / MCP23017 / XL9535 / PCF8574), AUX relay automation, temperature / humidity
sensors, tamper input, UPS input, battery measurement.

## Quick start

```bash
cd server
docker compose up -d --build
docker logs powermanager      # shows the one-time setup token
```

Open `http://<server-ip>:5000`, create the admin, then **Add New Meter**. Make the meter's `config.h` on the server
(**Firmware → Generate config.h**), flash it with the Arduino IDE ([firmware/README.md](firmware/README.md)) and
pair it with the code or QR. The server is normally published through a Cloudflare Tunnel
([server/README.md](server/README.md)).

## Development

```bash
make venv            # server/.venv with the server + test requirements
make test            # server tests + firmware PC tests
make run             # development server on http://localhost:5000
make firmware-setup  # once: ESP32 core + libraries for arduino-cli
make firmware        # compile both sketches and the build variants
```

CI (GitHub Actions, `.github/workflows/ci.yml`) runs the same on every pull request; pushing a tag `v*` publishes
the server image (`release.yml`). Details: [docs/handover.md](docs/handover.md).

## Documents

| Doc | Content |
|-----|---------|
| [docs/handover.md](docs/handover.md) | **Start here when taking over:** state of the project, how to work on it, rules that keep it consistent, release steps, open items |
| [docs/architecture.md](docs/architecture.md) | System overview, key decisions, deployment (Docker + Cloudflare Tunnel) |
| [docs/protocol.md](docs/protocol.md) | Pairing and the message contract between meter and server (WebSocket, MQTT, mesh) |
| [docs/data-model.md](docs/data-model.md) | Server database tables, retention, roll-ups |
| [docs/server-ui.md](docs/server-ui.md) | Server pages, fields, validation rules, roles |
| [docs/firmware.md](docs/firmware.md) | ESP32 behaviour: setup, tasks, enforcement, LED, buffering, pin map, libraries |
| [docs/multichannel.md](docs/multichannel.md) | Multi-channel AC meter: hardware, Shared / Independent modes, AUX relays |
| [docs/mesh.md](docs/mesh.md) | Mesh over ESP-NOW: roles, security, radio details, test plan |
| [CHANGELOG.md](CHANGELOG.md) | What changed per version |
