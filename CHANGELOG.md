# Changelog

Server and firmware are versioned separately; from 1.0.0 the server needs firmware 1.0.0 or newer.

## 1.0.0 (server and firmware)

Baseline for hand-over.

- **Breaking:** support for older firmware removed on both sides. Firmware 1.0.0 does not read settings saved by
  older versions (rbAmp kWh counters / relay states restart, AUX automation is re-sent by the server after the
  meter's configuration is saved once). The server warns about meters older than 1.0.0.
- `config.h` of both sketches reorganized: [1] board info, [2] enable flags, [3] pin out, [4] options.
- Meter Configuration page in tabs with one Save bar; Firmware page with a separate **Generate config.h** page.
- Developer tooling: `Makefile`, `firmware/tools/{build,setup_arduino,host_tests}.sh`,
  `server/requirements-dev.txt`, `server/docker-compose.dev.yml` (server + MQTT broker), GitHub Actions CI and
  release workflows, `docs/handover.md`.

## Before 1.0.0 (development history, newest first)

- UPS signal input (event, notification, historical event); AUX relay events for UPS and battery %.
- Backup Wi-Fi with automatic return to the main network.
- AUX relays: 14 schedules by week day or date, events for tamper / trip / voltage / Wi-Fi / server, "any channel".
- `config.h` generator on the server.
- MQTT link (MQTT over WebSocket, signed messages, auto link at pairing, WebSocket fallback); change Wi-Fi and
  broker from the server; Info page auto refresh.
- MCP23017 relay driver; shared relay driver for GPIO / PCF8574 / XL9535; SHT20 / 3x / 4x and DS18B20 sensors.
- Sketches renamed to `PowerMeterPZEM` and `PowerMeterRBAmp`; several rbAmp boards with a channel table.
- Session summary, "Core data only", selected-summary CSV; fixes for lost session ends and kWh limit changes.
- Manager role, per-meter electricity price, positive watt only, per-channel viewers, reversed clamp warning.
- Multi-channel AC meter (rbAmp) with Shared / Independent modes, AUX relays, DRDY data mode.
- DC meter (PZEM-017), Telegram bot, mesh over ESP-NOW, tare, hardware change detection, data clean-up.
- First version: single-channel AC meter (PZEM-004T), pairing, sessions, limits, history, notifications.

The full history is in the git log.
