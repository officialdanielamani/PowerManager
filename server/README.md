# PowerManager Server

Flask + SQLite server for PowerManager meters, running in Docker. The design is in [`../docs`](../docs).

## Run with Docker (recommended)

1. Download the repository (green **Code → Download ZIP** on GitHub, then unzip) or `git clone` it.
2. Build and start:

   ```bash
   cd PowerManager/server
   docker compose up -d --build
   ```

3. Get the one-time **setup token** from the log:

   ```bash
   docker logs powermanager
   ```

4. Open `http://<server-ip>:5000`, enter the token and create the admin account.

Data (database + generated secret key) lives in `server/instance/`. Back up that folder.

To update later: replace the files with the new version, then run `docker compose up -d --build` again.

### Settings (`.env`, optional)

Copy `.env.example` to `.env` next to `docker-compose.yml`.

| Variable | Default | Meaning |
|----------|---------|---------|
| `SECRET_KEY` | generated, saved in `instance/secret_key` | Signs login cookies |
| `SETUP_TOKEN` | random, printed in the log | Required by the first-run setup page. `off` disables it (LAN only) |
| `GUNICORN_THREADS` | `100` | Each connected meter holds one thread; raise it for more than ~90 meters |

Build behind a registry mirror: `docker compose build --build-arg BASE_IMAGE=mirror.gcr.io/library/python:3.11-slim`.

## Cloudflare Tunnel

- Point the tunnel's public hostname at `http://<docker-host>:5000` (or `http://powermanager:5000` if `cloudflared` runs in the same Docker network).
- In PowerManager: **Settings → Server** → Address = your public hostname, Port = empty, Secure (TLS) = on. This goes into the pairing QR.
- WebSockets work through the tunnel by default. If you use **Cloudflare Access**, add a *Bypass* policy for `/ws/device` and `/api/v1/pair`, otherwise meters get a login page.

## MQTT (optional)

Meters can reach the server through an MQTT broker instead of the WebSocket (useful when the broker is the only
thing published, e.g. behind Cloudflare). **Settings → MQTT**: broker address for the server, the address the
meters use (MQTT over WebSocket), *Test connection*. Meters get the broker when they are paired; every message is
signed with the meter's own key. Details: [`../docs/protocol.md`](../docs/protocol.md) §7.

## Development

From the repository root, `make venv`, `make test`, `make run` do the steps below (see the `Makefile`).

```bash
cd server
python -m venv .venv && . .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements-dev.txt                # server requirements + pytest + test broker
python wsgi.py                                     # http://localhost:5000 (setup token is printed)
python -m pytest -q
```

Tests use a temporary database per test (`tests/conftest.py`); nothing touches `instance/`.

### Test environment with an MQTT broker

```bash
docker compose -f docker-compose.dev.yml up --build
```

Server on `http://localhost:5001` (own data folder `instance-dev/`, setup token check off) and a Mosquitto broker
on 1883 (TCP) and 9001 (MQTT over WebSocket). The file's header says what to enter in Settings → MQTT.

### Test without hardware: simulated meter

```bash
pip install simple-websocket
# Dashboard -> Add New Meter, copy the pairing code, then:
python tools/sim_meter.py pair --server http://localhost:5000 --code ABCD-EFGH
python tools/sim_meter.py run --load 900
```

While it runs, type `trip`, `tamper_open`, `tamper_close`, `ac_lost`, `ac_restored`, `load 1500`, `send` or `quit`.
`--script "20:trip,40:tamper_open"` runs actions on a timer; `--energy-x 100` makes energy grow faster to test kWh quotas.
Use `--state other.json` to simulate several meters at once.

Multi-channel meter: `python tools/sim_meter_mc.py --state mc1.json pair --server http://localhost:5000 --code ABCD-EFGH --channels 2`,
then `python tools/sim_meter_mc.py --state mc1.json run --load 600,1200`. Both simulators use the WebSocket link.

## Layout

```
app/
  __init__.py          app factory, setup-token guard, startup tasks
  models.py            SQLAlchemy models (docs/data-model.md)
  routes/              auth, dashboard, meters, settings, device_api (pairing + WebSocket)
  services/            ingest (protocol handling), incidents, commands, devconfig, auxcfg, history,
                       rollup, notify (in-app + Telegram), scheduler, hub (live connections),
                       mesh, mqtt_bridge, access (roles), cfggen (config.h generator)
  firmware_cfg/        copies of the two firmware config.h files, used by the generator
  migrations.py        in-place database upgrades at start-up
  templates/, static/  Bootstrap UI (no build step)
  static/vendor/       Bootstrap 5.3.3, Bootstrap Icons 1.11.3, Chart.js 4.4.1 (MIT, committed;
                       scripts/fetch_vendor.py re-downloads them if needed)
tools/                 simulated meters (sim_meter.py, sim_meter_mc.py)
tests/                 pytest
dev/, docker-compose.dev.yml   test environment with an MQTT broker
```
