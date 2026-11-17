# Changelog

Server and firmware are versioned separately; from 1.0.0 the server needs firmware 1.0.0 or newer.

## Unreleased (1.1.0)

- **Breaking:** server 1.1.0 needs meter firmware **1.1.0** (`MIN_FW`); older firmware is no longer supported. In the rbAmp
  `CHANNELS` table (`config.h`, hardware profile, generator) a channel's relay is now **relay board + output** (`{board,
  input, clamp, relayBoard, relayOutput}`, e.g. Board A relay 2) instead of one number across all boards. Existing
  `config.h` files must be regenerated (Firmware → Generate config.h) or edited: `{1, 2, SCT_30A, 2}` becomes
  `{1, 2, SCT_30A, 1, 2}` when relay 2 is output 2 of the first relay board. AUX relays keep their number across boards.

- Boards are now ESP32-S3 **N16R8** (16 MB flash, octal PSRAM): custom `partitions.csv` in both sketches (64 KB NVS,
  two 6 MB app slots for OTA, 3.8 MB LittleFS). Build scripts, CI and README use `FlashSize=16M,PSRAM=opi,PartitionScheme=custom`.
  **One USB flash is needed** to change the partition table of an existing meter (readings in the offline buffer are lost).
- Server: hardware profile of rbAmp meters (rbAmp addresses, relay boards, channel table with clamp models) with
  live clamp change, delayed reboot, rollback, I2C scan and rbAmp address commands (`docs/protocol.md` §12,
  `services/hwprofile.py`); AUX event source "Wait for reboot"; a channel count change caused by a profile no longer
  archives the meter. Up to 16 channels. `sim_meter_mc.py` simulates it (`--bus`).
- Firmware → **Update Device I/O** page (select meter, rbAmp boards, relay boards, channel table with clamp dropdown, Scan
  Address, Update Clamp Model, Save & Apply with countdown / Cancel / rollback status, rbAmp address wizard) and
  **Generate config.h → Start from the hardware of a meter**. See `docs/server-ui.md`.
- rbAmp firmware 1.1.0: run-time hardware profile (`src/hwprofile.*`, `hwmap.h`, `relaymap.h`): stored in NVS, delayed
  restart (AUX event "Wait for reboot"), confirm check, rollback and crash-loop protection, live clamp change, commands
  `i2c_scan`, `rbamp_addr`, `hw_profile`, `hw_cancel`; sessions end with reason `hw_change` before the restart.
  PC test: `firmware/tools/hwprofile_test`. The PZEM sketch only gets the new AUX event source (version 1.1.0).

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
