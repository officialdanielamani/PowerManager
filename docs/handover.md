# Handover guide

For whoever takes the project over. Read this first, then `architecture.md` and `protocol.md`.

## 1. What the project is

- **Server** (`server/`): Flask + SQLite in one Docker container. Web UI for admin / manager / viewer accounts,
  pairing, configuration pushed to the meters, history, notifications (in-app + Telegram), a `config.h` generator.
  Meters connect by WebSocket (`/ws/device`) or through an MQTT broker (`app/services/mqtt_bridge.py`).
- **Firmware** (`firmware/`): two Arduino sketches for the ESP32-S3.
  `PowerMeterPZEM` = one channel (PZEM-004T AC or PZEM-017 DC), `PowerMeterRBAmp` = 1–16 channels (rbAmp modules).
  The meter enforces every limit itself and keeps working offline; the server owns the configuration.
- **Versions**: server 1.0.0 (`server/app/__init__.py`), firmware 1.0.0 (`FW_VERSION` in each `config.h`).
  The server only supports firmware ≥ `MIN_FW` (`server/app/services/devconfig.py`).

## 2. What is verified, and what is not

| Area | State |
|---|---|
| Server logic | Automated tests (`server/tests`, 138 tests) + simulated meters (`server/tools/sim_meter*.py`) |
| Server UI | Checked by hand / with a headless browser during development; no automated browser tests in the repo |
| Firmware build | Both sketches and the variants in `firmware/tools/build.sh` compile on Arduino-ESP32 3.3.5 |
| AUX automation | PC test against the real `auxrelay.cpp` (`firmware/tools/auxrelay_test`) |
| MQTT link | PC test: the firmware's `mqttlink.cpp` + a real broker + the server bridge (`firmware/tools/mqttlink_test`) |
| Basic metering, relays, sessions on real meters | Used on real hardware by the project owner during development (earlier firmware versions) |
| **Not yet tried on real hardware** | MQTT link (also through Cloudflare / TLS), backup Wi-Fi switching, UPS input, battery % event, AUX schedules by date and the newer event sources, changing Wi-Fi from the server, firmware 1.0.0 as a whole after the legacy clean-up |
| Mesh | Has its own manual test plan in `mesh.md` |

Treat the "not yet tried" row as the first test list on a bench meter before a field roll-out.

## 3. Working on it

```bash
make venv            # server/.venv with server + test requirements (server/requirements-dev.txt)
make test            # pytest + firmware PC tests (needs g++, libssl-dev, git)
make run             # dev server on http://localhost:5000, setup token printed in the terminal
make firmware-setup  # once: arduino-cli core esp32 3.3.5 + ArduinoJson 7.1.0 + WebSockets 2.7.2
make firmware        # compile both sketches + variants (dc, example, all-parts, ws-only, mqtt-only)
make docker          # build the server image
```

- **Simulated meters**: `server/tools/sim_meter.py` (one channel) and `sim_meter_mc.py` (multi-channel); usage in
  `server/README.md`. They speak the WebSocket protocol only.
- **Test environment with an MQTT broker**: `cd server && docker compose -f docker-compose.dev.yml up --build`
  (server on port 5001 with its own data folder `instance-dev/`, Mosquitto on 1883 and 9001 / WebSocket).
- **Firmware by hand**: Arduino IDE, settings in `firmware/README.md`. `firmware/tools/build.sh <sketch> [variant]`
  does the same build with arduino-cli and leaves the `.bin` in `firmware/build/`.

## 4. CI / CD

| Workflow | When | What |
|---|---|---|
| `.github/workflows/ci.yml` | every pull request, pushes to `main` / `master`, manual | server tests (Python 3.11 and 3.13), firmware PC tests, firmware compile matrix (7 builds), Docker image builds and answers `/healthz` |
| `.github/workflows/release.yml` | tag `v*`, manual | tests, then the server image to `ghcr.io/<owner>/powermanager` (`<version>`, `<major>.<minor>`, `latest`) |

The workflows were written with the project but have **not run on GitHub yet** (the scripts they call were run
locally). Expect to fix small things on the first run, e.g. the Arduino cache or package permissions for GHCR
(repository *Settings → Actions → General → Workflow permissions*: read and write, for the release workflow).
`docker-compose.yml` builds the image locally; to use the published image instead, replace `build: .` with
`image: ghcr.io/<owner>/powermanager:<version>`.

Firmware is not released as binaries: each meter is compiled with its own `config.h`. OTA is not implemented
(the Firmware page only has the generator).

## 5. Rules that keep the project consistent

1. **Shared firmware files.** Most files in `PowerMeterPZEM/src` and `PowerMeterRBAmp/src` must be identical
   (`firmware/tools/check_shared.py` lists them). Edit one, copy to the other, run the check. Files that differ on
   purpose: `app_state`, `control`, `storage`, `net.cpp`, `web.cpp`, `webui.h`, `led.cpp`, `hwmap.h`.
2. **config.h and the generator.** `server/app/firmware_cfg/{pzem,rbamp}_config.h` are copies of the two
   `config.h` files; the generator (`app/services/cfggen.py`) replaces `#define` values in them. After changing a
   `config.h`: copy it there (the check script and `tests/test_cfggen.py` fail otherwise), and if you added an
   option that users should set, add it to `cfggen.py` + `templates/firmware_config.html`. Keep the four parts
   ([1] board info, [2] enable flags, [3] pin out, [4] options). Regenerate `firmware/examples/*.h` with the
   generator when the layout changes.
3. **Protocol changes** go into `docs/protocol.md` first. What a meter can do is announced in `hello.caps`
   (`net.cpp`); the server shows options by capability, never by version number.
4. **Versions.** Bump `FW_VERSION` in both `config.h` files (and the copies) for any firmware change, and
   `__version__` in `server/app/__init__.py` for a server release. Raise `MIN_FW` only when the server really
   stops working with older firmware, and say so in `CHANGELOG.md`.
5. **Database.** No migration framework: `server/app/migrations.py` adds columns in place at start-up. A new
   column needs a line there, or existing installations break. The data lives in `server/instance/` (back it up).
6. **One gunicorn worker.** Live meter connections are held in memory (`services/hub.py`), so the server must run
   with a single worker and many threads (`gunicorn.conf.py`).
7. **Secrets.** Meter secrets, link keys, the Telegram token and the MQTT password are stored in the database;
   Wi-Fi passwords sent to a meter are wiped from the command table once acknowledged. Nothing secret belongs in
   the repository; `.env` is ignored.
8. **Tests with every change.** Server behaviour: `server/tests`. Firmware logic that can run on a PC: add to the
   host tests rather than relying on a compile only.

## 6. Where things are

| Need to change… | Look in |
|---|---|
| What a message from a meter does | `server/app/services/ingest.py` (handlers per message type / event) |
| What is sent to a meter | `services/devconfig.py` (config), `services/commands.py` (commands), `services/auxcfg.py` (AUX) |
| Who may see / do what | `services/access.py`, `routes/decorators.py` |
| A page | `server/app/templates/…`, `static/js/app.js` (live updates), routes in `routes/` |
| History / charts / CSV | `services/history.py`, `services/rollup.py` |
| Notifications | `services/notify.py`, `services/telegram_bot.py`, types in `devconfig.NOTIFY_TYPES` |
| Limits, sessions, relays on the meter | `firmware/*/src/control.cpp` |
| Wi-Fi, server link, commands on the meter | `firmware/*/src/net.cpp`, `mqttlink.cpp`, `mesh.cpp` |
| The meter's own web page | `firmware/*/src/web.cpp`, `webui.h` |
| Hardware wiring tables | `config.h`, `src/hwmap.h`, `src/relaymap.h` |

## 7. Release checklist

1. `make test` and `make firmware` pass; CI is green.
2. Versions bumped (rule 4), `CHANGELOG.md` updated.
3. Flash one real meter of each type with the new firmware; pair, read, switch a relay, run one session.
4. Back up `server/instance/` on the production host, then `docker compose up -d --build`.
5. Tag `vX.Y.Z` to publish the image.

## 8. Open items

- Hardware verification of the list in section 2.
- OTA firmware update (page placeholder only).
- Idea discussed, not built: sending hardware settings (clamp model, channel table) from the server so they need
  no recompile; enable flags would still be compile-time.
- The repository has no `LICENSE` file; the owner should choose one before publishing more widely.
- `docs/firmware.md` and `docs/architecture.md` describe the original single-channel design in the most detail;
  later features are documented in `protocol.md` (sections 7–11), `multichannel.md` and `server-ui.md`.
