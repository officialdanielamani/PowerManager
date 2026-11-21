# Changelog

Server and firmware are versioned separately; from 1.0.0 the server needs firmware 1.0.0 or newer.

## Unreleased (1.1.0)

- **Firmware list, OTA update and USB flash from the browser** (design + rules: `docs/protocol.md` §14): Firmware page lists firmware
  (admin: Add New Firmware / Edit / bulk Delete; manager: Info and Flash). The admin uploads the zip of the Arduino IDE export; the server
  keeps the app image and the USB files and reads the firmware **tag** (type / version / hardware) from the image. *Flash* a meter:
  search a meter, firmware and meter side by side with a verdict (blocked: offline / other type / other partition layout / too big / no
  OTA support; warnings: downgrade, running session), then the meter downloads the image over HTTP(S) with a one-time token, checks SHA-256
  and tag, restarts and must reach the server again, otherwise it goes back to the old firmware (rollback by the app, NVS). Sessions end
  when the update starts. *By USB (browser)*: Web Serial + esptool-js (vendored), no online meter needed, Update or Erase all flash first
  (typed device ID / chip MAC), needs HTTPS (reverse proxy) or localhost. New notification **Meter restart** (hardware change + update).
  Firmware: new `ota.cpp/.h/otatag.h` (both sketches), `hello.fw_type / hw / ota / caps.ota`, command `ota`, events `ota_*`, session end reason
  `restart`. **Meters need one USB flash (or Erase All Flash) with this firmware before they can be updated over the air.**
  Tests: `firmware/tools/ota_test`, `server/tests/test_firmware.py`, browser test (upload, OTA with the simulator, delete).
- **Zero & Tare for all meters, per channel** (design: `docs/protocol.md` §13): Configuration → *Tune* tab (between Channels and AUX relays; Calibration greyed out; tare minimum depends on the clamp range) with a sensor check (>= 10
  readings / 10 s, stable), *Set zero* (sensor offset with no load, subtracted on the meter), *Set tare* (a connected load that counts as 0;
  tared values in the views, CSV and the meter's limits), Clear. Replaces the single-channel tare of the Configuration form (the
  old value is migrated to channel 0); multi-channel meters have an independent zero / tare per channel. New: command / event
  `sensor_check`, `cfg.ch[i].zero / tare`, `cfg.tare_limits`, `kwh_t` in session progress, rbAmp per-channel tare accounting.
  Calibration (known loads) is designed and held. Test: `firmware/tools/sensorcheck_test`, browser test in `server/tools/e2e`.
- Relay boards of a hardware profile have a stable `id` (Board A…H): removing Board A leaves Board B as Board B; the
  generator closes the gap for `config.h`, which counts by position.
- Boards are N16R8: `OUTBOX_SEGMENTS` 100 (about 5000 buffered items, ~800 KB of the 3.8 MB LittleFS), `HW_NAME`
  `esp32s3-n16r8`, a note about the PSRAM / flash pins in `config.h`.
- Test tools in the repository: `firmware/tools/hwprofile_test`, `firmware/tools/syntax_check` (ESP32 sources against
  stubs), `server/tools/e2e` (browser test against a real server + simulated meter), `make syntax-check`, `make e2e`; CI runs
  the browser test as its own job (`e2e` in `ci.yml`).
- **Fix:** an earlier `partitions.csv` of this branch moved the app to 0x20000 and did not boot with the Arduino upload
  (`No bootable app partitions`): the table is back to the standard head, `check_shared.py` guards it. `hwprofile.cpp`
  checks that NVS took the profile (`err: "storage"` / rollback).
- I2C fault handling: relay board pin directions are written with every state write (MCP23017 / XL9535 boards that lose power or are
  plugged in later work again by themselves), a stuck bus is freed (`i2cbus.cpp`: SCL clocking + STOP + restart, `info.i2c_recov`),
  an rbAmp module that is silent for 5 s is probed again. Test: `firmware/tools/i2c_test`.
- **Breaking:** server 1.1.0 needs meter firmware **1.1.0** (`MIN_FW`); older firmware is no longer supported. In the rbAmp
  `CHANNELS` table (`config.h`, hardware profile, generator) a channel's relay is now **relay board + output** (`{board,
  input, clamp, relayBoard, relayOutput}`, e.g. Board A relay 2) instead of one number across all boards. Existing
  `config.h` files must be regenerated (Firmware → Generate config.h) or edited: `{1, 2, SCT_30A, 2}` becomes
  `{1, 2, SCT_30A, 1, 2}` when relay 2 is output 2 of the first relay board. AUX relays keep their number across boards.

- Boards are now ESP32-S3 **N16R8** (16 MB flash, octal PSRAM): custom `partitions.csv` in both sketches (standard
  20 KB NVS / otadata 0xe000 / app 0x10000 as the Arduino upload requires, two 6 MB app slots for OTA, 3.9 MB LittleFS). Build scripts, CI and README use `FlashSize=16M,PSRAM=opi,PartitionScheme=custom`.
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
