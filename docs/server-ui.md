# Server UI

## Validation rules

| Field | Rule |
|-------|------|
| Username | 3–32 chars, `a-z 0-9 _ . -`, case-insensitive (stored lower-case), unique |
| Password | 10–128 chars, at least 1 upper, 1 lower, 1 digit, 1 symbol; must match Confirm |
| Email | valid format, ≤ 254 chars |
| Phone | E.164: `+` then 8–15 digits (e.g. `+60123456789`) |
| Meter Nick Name | 1–40 chars |
| Meter Info | 0–200 chars |

Login: rate limited to 5 tries per minute per IP; view-only accounts also lock after N failed logins (Settings → Users). Changing the password ends other sessions.

## Pages

### `/setup` (first run only)
Username, Password, Confirm Password, Phone, Email (all required) + Setup token if `SETUP_TOKEN` is set → creates the admin → goes to the dashboard.

### Dashboard `/`
- A grid of **meter cards**: name, info, online dot, relay state, live W, then two labeled bars:
  - **Energy**: session kWh used / limit (yellow once the limit is reached)
  - **Time** (only when the session has a time limit): time left, e.g. "1 h 29 min left (auto renew)", counting down every second; grey "Session ended" / "Ended, renews in …" after the end
  - **Session cost** (if a price is set), status, ongoing incidents, *Pending sync*, last seen.
- Cards update live (polling every 5 s; the time bar ticks every second using the server's clock).
- **Admin:** a checkbox on each card. When meters are selected, a bar appears: *N selected · Select all · Clear · **[Renew Now]** · **[Bulk Configuration]***.
  - **Renew Now** starts a new session on every selected meter (offline meters renew when they reconnect).
  - **Bulk Configuration** opens the page below.
- Left nav: Dashboard, **[Add New Meter]** (admin), Notifications, **Firmware** (admin; placeholder for OTA updates), Settings, Logout.
- View-only users see only the meters linked to them (or all, if *View all meters* is set), without checkboxes.

### Bulk Configuration (admin) `/meters/bulk/configure?ids=…`
- **Device ID**: the selected meters (name, ID, online dot).
- Sections, each with a **Change** switch. Only switched-on sections are written, so other settings (and each meter's name/info) stay as they are on every meter. A *values differ* badge shows when the selected meters currently disagree (the form shows the first meter's values):
  Send data every · kWh limit (+ what happens at the limit) · Max current (max = lowest rating among the selected meters) · Session time limit & auto renew · Relay off when offline · Linked users (*add* / *remove* / *replace with exactly these*).
- Applying pushes a new config version to each changed meter and lists each meter's result live: *Confirmed by meter* / *Pending sync (offline)* / *Rejected*.

### Add New Meter (admin)
QR + Server address + Port + Secure + Pairing code + 10-min countdown + [Regenerate]. Redirects to Configure when paired. Shows a warning if *Settings → Server* is not set yet.

### Configure Meter Device (admin)
Shows a **Synced / Pending sync** badge.

| Section | Fields |
|---------|--------|
| General | Device ID (read-only), Nick Name, Info, Send data every [10 s – 1 h] |
| Actions | [Renew Now] · [Set to Default] (resets config only, keeps name/info/history) · **[Disable / Unsync / Remove]** (dialog explaining each option, "I understand" tick, Device ID typed for Remove) |
| Tune | between *Channels* and *AUX relays*: Zero, Tare (and Calibration, greyed out for now) per channel, see *Configuration → Tune* below. Also has **Use tared values for limits** (max current and kWh limit on the meter) |
| Data clean-up | [ ] Automatically delete data if session more than [120] days ago · [ ] Automatically delete Historical Events more than [120] days ago |
| Set Limit | In this order: **Max kWh per session** + *When the kWh limit is reached*: ( ) Session run as usual · (•) Off relay, but session continue (default) · ( ) End session — **Session time limit** (none / duration D-H-M / daily HH:MM), Auto renew, Relay off when offline — **Max Amp** (0 = off; latched until Reset Trip) — **Operating Voltage Range** Min–Max V + *If out of range* (same 3 choices) — **Tamper Protection** [ ] enable + *If tamper detected* (same 3 choices) — **Sensor Timeout**: if the sensor does not respond or errors for [10] s (0 = off) + the same 3 choices (default: Off relay, but session continue) |
| Link this meter to users | Checkbox per view-only user (server-only, never sent to the meter) |

Notification options are **not** here any more: see *Notification Options* below.

[Save] → stores config v+1 → sends to device → waits up to 15 s for `cfg_ack`:
- ack ok → "Saved and confirmed by device", close page → Meter Info.
- rejected → show the device's error, keep the form open.
- device offline / timeout → "Saved — will apply when the device reconnects" (Pending sync).

Each meter has three tabs: **Info** (everyone with access), **Notification Options** (everyone with access, per account), **Configuration** (admin).

### Re-paired unsynced meter
The Configuration page shows: *This hardware was used before as "Dryer A" and was unsynced* → **[Continue as "Dryer A"]** / **[Make it a new meter]**.

### Disabled meter
Info page banner: *Disabled since … relay off, no session, read-only* with **[Enable]** and **[Unsync / Remove]** (admin). **Enable** does not start a session: press **Renew Now** when ready. Configuration is blocked; dashboard card is greyed with a *Disabled* badge.

### Notifications page
Own notifications only: select (checkbox, select all) → **Delete selected**, or **Delete all**; *Mark all read*. Settings → Account → *My notifications*: [ ] Automatically delete notifications more than [30] days ago.

### Notification Options (admin and linked viewers, per account)
Switches: kWh exceeded · Voltage out of range · Session renewed · Max amp · Panel open · No voltage · Disconnected for [X] min · Reconnected. They apply only to the logged-in account.

### Meter Info (view-only; admin and viewers)
- Device ID, Nick Name, Info, kWh per session, next session reset (display TZ), **session cost** (if a price is set), **session started, time elapsed, time left** (live, ticks every second; "Session ended" once a limit ended it), auto renew yes/no, last online, firmware.
- **[Raw | Tared]** switch in *Latest data* (only when the meter has a tare on any channel; multi-channel meters: each channel is tared with its own tare, the whole-meter numbers use their sum; remembered per browser): latest data, session kWh/balance/cost, history chart + stats and the Sessions table switch between raw and tared values. Dashboard cards show tared values when a tare is set.
- **[Get meter info]** (admin) in the *Meter* card: asks the meter for IP, Wi-Fi, MAC, battery voltage + % (if enabled), free heap, PSRAM, firmware, hardware name, `HW_INFO`, chip, uptime; shown in a dialog.
- Historical Events (admin): checkboxes + **Delete selected**.
- **[Get Data Now]** in *Latest data* (admin and viewers): asks the meter for a reading now and waits up to 15 s for it (rate limited to once per 5 s per meter).
- Latest data: V, A, W, PF, Hz, session kWh used / remaining.
- Admin-only buttons: Relay ON/OFF, [Renew Now], [Reset Trip], [Send Data Now], [Configure].
- History chart (V / A / W / kWh) with range: 1 h, 1 day, 1 week, 1 month, all time, custom; stats: min / max / avg.
- [Export CSV] exports exactly the range currently shown (admin and viewers).
- **Historical Events** (from the `incident` table, newest first):

  | Type | Started | Ended | Duration | Details |
  |------|---------|-------|----------|---------|
  | Power disconnected | 2026-11-03 14:02:11 | 2026-11-03 14:20:45 | 18m 34s | – |
  | Offline | 2026-11-02 23:10:05 | 2026-11-02 23:12:40 | 2m 35s | – |
  | Power trip | 2026-11-01 19:44:02 | 2026-11-01 20:01:10 | 17m 08s | 10.6 A > 10.2 A · reset by admin (server) |
  | Tamper | 2026-10-30 09:15:00 | **Ongoing** | 3d 4h | – |

  - Times are in the display timezone; ongoing incidents are highlighted and their duration keeps counting.
  - Filter by type (Power / Offline / Trip / Tamper, multi-select) and date range; paged, 25 per page.
  - [Export CSV] exports the filtered list (UTC + local time, duration in seconds).
  - `~` before a time marks an estimated timestamp.
- Dashboard meter cards show a small badge for each **ongoing** incident (⚡ no power, ⛔ trip, 🔓 tamper, offline dot).
- Session history (start, end, duration, kWh used, **cost**, end reason, **[Historical]** = show exactly this session's time range in the Historical data chart (custom range; the chart's Export CSV follows it), **[CSV]** = power data of that session); the history stats show the cost of the selected range as a second tab next to Historical Events.

### Settings
| Tab | Content |
|-----|---------|
| Account | Change username, password, phone, email (current password required) · My Telegram chat ID + [Send test] |
| Server (admin) | Public address, port, TLS · Display timezone (IANA list, shows its UTC offset) · Raw data retention. (The electricity price is set per meter.) |
| Users (admin, manager) | Admin: every manager and view-only account; Add account with **Role** (Viewer / Manager) and, for a viewer, **Belongs to** (Admin / a manager). Manager: only its own view-only accounts, linked only to its meters. View-only accounts (per user: **Enable download .csv**, **Show price**, both off by default; enforced on the server): select + Enable/Disable/Unlock selected, Enable all / Disable all · per user: meters they can see (or *View all meters*), reset password (also unlocks), enable/disable, delete · Add account (with meters) · Login protection (max failed logins, lock minutes) |
| Notifications (admin) | Telegram on/off + bot token (each user adds their own chat ID). The token is write-only like a password: after saving the field is empty with a `••••` placeholder; leave it empty to keep the saved token, or tick *Remove the saved token* |

### Firmware (admin, manager)
**List of firmware** with one **Generate config.h** button; the list itself is empty for now (OTA updates are
coming in a later version). A warning shows when some of your meters run firmware older than 1.0.0.

**Generate config.h** (own page, `/firmware/config`): choose the meter type, power sensor boards / channels, relay
boards, temperature / humidity sensors, other parts (RTC, tamper, UPS input, battery) and defaults (WebSocket /
MQTT, mesh, setup Wi-Fi password, pins, intervals), click *Generate*, then *Copy all* or *Download* and replace
the sketch's `config.h`. The page checks the choices (I2C address or GPIO used twice, relay used by two channels,
too many outputs for a board) and lists the relay numbering, the AUX relays and the I2C addresses. The result is
the config.h of the firmware version shipped with the server (`server/app/firmware_cfg/`, kept equal to
`firmware/*/config.h` by `tests/test_cfggen.py`). The meter's own page has a greyed-out **Check OTA Update** button.

## Password fields

Every password field (server and the meter's own page) has an **eye** button to show / hide what was typed.

## AC and DC meters

The meter type comes from the firmware when it pairs (no setting). Dashboard cards and the meter pages show an **AC** or **DC** badge; Get meter info shows *AC Power Sensor* / *DC Power Sensor* (sensor models are not shown). DC meters have no power factor / frequency: the Latest data box and CSV export leave them out, and V / A are shown with 2 decimals. Configuration is the same for both.

## Read / send interval

Configuration → General: **Read data every [X] s and send every [Y] s** (read 5 s – 1 h, send 10 s – 1 h, read ≤ send). The meter records a reading every X s and sends the buffered readings every Y s (Get Data Now and events go at once). Also in Bulk Configuration (*Read / send data*).

## Sessions tab: select and delete

Admin: tick ended sessions (or the header box for all) → **Delete selected**. Only the session record is deleted (power readings stay); the running session has no checkbox. Session numbers are not renumbered.

## Meter Status notification

Meter → Notification Options → **Meter status** (disabled / enabled / unsynced / synced / removed). **On by default** for every account that can see the meter, until the account switches it off. *Synced* = hardware that was unsynced pairs again. A *removed* notice is kept after the meter's data is deleted.

## Meter clock / RTC

**Get meter info** shows the meter's clock (and where its time came from) and, with a DS3231 fitted, the RTC's own time, both in the display time zone. The meter's own page shows the same next to its date/time.

## Telegram bot (interactive)

With Telegram enabled (Settings → Notifications) the server also answers messages to the bot (long polling: no webhook or public URL needed). Only chats whose ID is saved in an account (Settings → Account → My Telegram) get answers; an unknown chat is told its chat ID. Each account sees only its meters.

| Message | Answer |
|---------|--------|
| `/start` | Keyboard with **📋 List** and **⚡ Now Reading** |
| 📋 List (`/list`) | Every meter: online dot, Device ID, nick name, AC/DC, power now |
| ⚡ Now Reading | Buttons to pick a meter → *Get Data Now* on that meter, waits up to 12 s, then V / A / W (PF / Hz for AC), energy, session kWh (+ cost if the account may see prices) |
| `/now <Device ID or nick name>` | Same, without the buttons |

## Multi-channel AC meters

Detected from the meter firmware at pairing (`caps.mc`); see [multichannel.md](multichannel.md).

- **Configuration → Set Limit → Configuration mode:** **Shared** (default: one session and one kWh limit for all channels) or **Independent** (each channel its own session, kWh limit, time limit, auto renew). Changing the mode ends the running session(s).
- **Configuration → Channels:** per channel a name, Max A (both modes) and, in Independent mode, kWh limit + action and session time limit.
- **Configuration → General → Read data when DRDY:** limits checked on every 100 ms window; raw readings stored at the read interval (1 s minimum instead of 5 s).
- **Info page:** a *Channels* panel (power, current, PF, kWh counter, relay ON/OFF and Reset Trip per channel; Renew / End per channel in Independent mode); header **Renew Now / End session** (Shared) or **Renew all / End all** (Independent). Latest data shows one voltage and a row per channel; the history chart and CSV have a channel picker; the Sessions tab has a Channel column.
- **Dashboard card:** total W plus one line per channel (W, relay, and in Independent mode its kWh bar).
- Bulk Configuration skips multi-channel meters (configure them one by one); Bulk Renew renews all their sessions.
- Telegram *Now Reading* lists every channel.

## Configuration page

- Header: **Renew Now** and **End session** (multi-channel Independent: **Renew all / End all**), like the Info page, plus an **Unsaved changes** badge after any edit (the browser also warns before leaving the page unsaved).
- Every section has its own **Save** (it saves the whole page).
- **Data clean-up** (server only, runs daily), three separate options: delete finished **Sessions**, **Historical Events**, **Historical Data** (power readings + hourly / daily summaries) older than N days.

## Delete Historical Data (Info page, admin)

**Delete data…** next to Export CSV: a date range, the past N minutes / hours / days, or (Danger zone, with a confirmation tick) all Historical Data of the meter. Sessions and Historical Events are not touched; summaries are rebuilt from the readings that remain.


## Managers, per-meter price, per-channel viewers

- **Manager** accounts (see architecture.md § Roles): own meters + own view-only accounts; no server settings.
- **Configuration → General** has **Manager** (admin only: None = admin only, or one manager who manages it too), **Electricity price** [currency][price] / kWh
  (default 0.25 for AC and DC; empty = no cost shown; locked into each session when it starts) and **Positive watt only**
  (default on: a negative power reading is stored as 0 W). Bulk Configuration can set the price too.
- **Link this meter to users** on a multi-channel meter: tick channels per user to show only those channels (none = the
  whole meter). Such a viewer sees only those channels on the dashboard, Info, history, CSV and Telegram; whole-meter session
  totals of a shared session are hidden.

## Reversed clamp warning

A clamp fitted the wrong way round reads negative power (e.g. −660 W), and the rbAmp counts no kWh for it, so that
channel's kWh limit cannot trigger. The server checks every reading **before** *Positive watt only* turns negatives into 0:
power ≤ −30 W with current ≥ 0.3 A for about 60 s flags the channel. It then shows a red **clamp reversed?** badge on
the dashboard card and the Info page (with a hint to turn the clamp over), and sends one **Meter status** notification.
Clear positive power (> 30 W) clears the flag. No-load noise (a few negative watts at ~0 A) is ignored.

## AUX relay automation

Configuration → **AUX relays** (any meter with AUX relays): per relay a name and the mode **Manual**, **Schedule**
or **Event**, and **Enable override**.

- **Schedule**: up to 14 lines (*Add schedule*; untick a line to remove it). Each line: ON / OFF, start → end, and
  **By day** (default: *All* / Mon … Sun) or **By date** (one date, optionally *Every year*), plus the state outside
  all lines. The first matching line wins, so put a date line (a holiday) above the week-day lines it overrides.
- **Event**: When [source] [sensor / channel] is [≥ / ≤ value | running / ended | active / not active] the relay is
  [ON / OFF], otherwise the opposite. Sources: Temperature, Humidity, Current, Power, kWh balance, Session, Tamper,
  Over-current trip, Voltage protection, No Wi-Fi, No server / internet. Current, Power, trip (and kWh balance /
  Session with independent channels) can watch one channel or **Any channel**; Current / Power also *All channels
  together*.
- Only what the meter has is offered: no Temperature without a temperature sensor, no Humidity without an SHT
  sensor, no Tamper without the tamper input.
- **UPS power** (active / not active) and **Battery %** (≥ / ≤ a value) appear when the meter's firmware has the
  UPS input (`UPS_ENABLE 1`) / the battery measurement (`BATTERY_ENABLE 1`).
- "No server / internet" is true while the meter is paired but has no link to the server (whatever the reason).

The Info page shows each AUX relay with its mode, a one-line description of the automation, *overridden* while a
manual switch wins, and ON / OFF buttons (hidden for automated relays without override). Details:
docs/multichannel.md § AUX relays.

## Session summary, Core data only, selected summary CSV

- Every `session_end` from current firmware carries a summary (min / max / average V, A, W). The Sessions tab shows
  *avg … W · max … W* under kWh used (hover: V and A).
- **Configuration → General → Data → Core data only (send when session ends)**: no readings every X s; the meter keeps
  only the session summary and sends it at the session end (cost from the meter's price). Limits run as usual; *Get Data
  Now* still gives a live reading. For meters that are mostly offline.
- **Historical Events / Sessions → tick rows → Download selected summary**: one CSV line per selected row (session: #,
  channel, start, end, duration, kWh used, cost, kWh limit, end reason, auto renew, V / A / W min / max / avg; event:
  type, start, end, duration, details). *Export CSV* / the row's *CSV* button still give the raw readings.
- A session whose `session_end` never reached the server is closed when the next session of the same meter / channel
  starts (end reason *Next session started*), so a meter never shows more than one running session per slot.
- Raising the kWh limit while a session is held at the old limit ("Off relay, but session continue") lets that session
  continue at once.

## MQTT

- **Settings → MQTT** (admin): enable, broker host / port / TLS / transport (WebSocket or TCP) / path / login / topic
  prefix for the *server*, and "How the meters connect" (public host / port / TLS / path) when the meters must use a
  different address than the server (e.g. server → broker inside Docker over TCP, meters → `wss://` through
  Cloudflare). *Test connection* tries the values in the form without saving them. The badge shows the live state.
- Saving sends the broker to every paired meter that supports MQTT; new meters get it while pairing (auto link).
- **Configure Meter → Connection** (admin / manager; meters with MQTT firmware): *Change Wi-Fi* (SSID typed by hand +
  password; the meter keeps the old Wi-Fi if the new one does not connect, and a Meter status notification tells the
  result) and *MQTT*: server broker / WebSocket only / own broker for this meter.
- **Configure Meter → Connection → Backup Wi-Fi**: a second network (another
  router, a phone hotspot). The meter tries main and backup in turn while it has no Wi-Fi, and on the backup it
  looks for the main network every 5 minutes and moves back when it is there. Saved without a test; *Remove*
  deletes it. A Meter status notification tells when the meter moves to the backup and back. It can also be set
  on the meter's own page (Settings → Wi-Fi).
- **Meter Info** shows *Link to server* (MQTT or WebSocket) and, on the backup network, "(backup)" behind the Wi-Fi name.
- The broker login alone is not enough to read as or control a meter: messages are signed with the meter's key
  (docs/protocol.md §7).

## Get Data Now: auto refresh

While the Info page is open and the meter is online, a fresh reading is requested when the last one is older than
about a minute, and the page updates as soon as it arrives. These readings are stored in Historical data like any
other, also with *Core data only*.

## UPS input

Firmware option (off by default; `UPS_ENABLE`, `PIN_UPS`, `UPS_ACTIVE_LEVEL` in config.h, also in the config.h
Generator): a signal from a UPS that tells "mains failed, running on the UPS". While it is active the meter shows
an **On UPS** badge, Historical Events gets an "On UPS power" entry (start, end, duration) and accounts with the
**UPS power** notification (Notification Options, on by default) are told when it starts and ends. It does not
switch the measured relays; use an AUX relay event for that.

## Configuration page layout

The Configuration page is split into tabs: **General**, **Limits & protection**, **Channels** (single-channel
meters: **Tare**), **AUX relays** (when the meter has them), **Data**, **Users** and **Connection**. One **Save**
bar at the bottom saves every tab at once; Connection has its own *Send to meter* buttons. The page remembers the
last tab, and a field the browser refuses opens its tab. A meter on firmware older than 1.0.0 shows a warning
that it must be updated.


## Firmware → Update Device I/O (hardware profile of an rbAmp meter)

Admin, and a manager for their own meters (a viewer gets 403). Reached from the Firmware page, next to **Generate
config.h**. Needs a multi-channel meter with firmware 1.1.0 or newer (`caps.hw_cfg`); older meters show a hint.
Protocol, rollback and rules: [protocol.md §12](protocol.md).

1. **Select meter**: the multi-channel meters the user manages.
2. **Status line**: Online / Offline and the profile state: *Compiled default* (nothing sent yet), *Waiting* (queued, e.g. offline),
   *Restart pending* with a countdown and **Cancel restart**, *Checking* (restarted, confirming), *Active* (v N), *Rolled back*
   (with the reason, e.g. `no_reply 0x51`), *Rejected* (the meter refused it). The page polls every 2.5 s.
3. **rbAmp boards**: one address (0x50–0x57) per module, board 1 first. **Relay boards**: type (MCP23017, XL9535 / TCA9535,
   PCF8574 / PCF8574A, ESP32 pins), address, outputs used, active high; the relay numbers of each row are shown.
   Relay boards are shown as **Board A [2C]**, **Board B [8C]** … (the number is the outputs used). The letter belongs
   to the board: **removing Board A leaves Board B as Board B** (its channels keep their relay); a new board takes the
   first free letter.
   **Channels**: rbAmp board, input 1–7, clamp (5 … 100 A or *Keep*), and the relay as **[Board A] [relay 2]** (or none);
   outputs no channel uses become AUX relays (listed under the table as "Board B relay 3").
4. **Scan Address**: the meter lists every I2C address that answers; rbAmp modules are confirmed, other addresses are labelled by
   range. The rows above then show *found / not found* badges. Needs the meter online.
5. **Update Clamp Model**: enabled when only clamp models differ from what the meter uses; applied at once, no restart.
   **Save & Apply**: enabled when anything else changed; restarts the meter after the delay (default 60 s, 10–600). Running
   sessions end (reason `hw_change`), relays go OFF. If a part does not answer, the meter rolls back by itself.
   A change in progress blocks both buttons. Errors (the rules of the protocol) are listed above the buttons; an AUX relay
   that the new profile removes must be cleared first.
6. **Give an rbAmp module a new address** (after a scan): connect only that module, pick its current and a free address, **Set
   address**. Refused while a session runs or if the new address is used.

**Generate config.h** has *Start from the hardware of a meter*: fills the rbAmp boards, relay boards and channels with what the
meter uses now, so a re-flash keeps changes made here. `config.h` counts relay boards by position (A, B, C … in order), so a
gap left by a removed board is closed in the generated file (the page says so).


## Configuration → Tune (all meters, per channel)

Admin, and a manager for their own meters; the *Tune* tab of the configuration page (the old `/calibration` address redirects there). One block per channel: header `CHx – name · clamp rating · now A`, then the rows **Zero**, **Tare** and **Calibration** (not available yet). One block per channel on a multi-channel meter (each channel
has its own zero and tare); the specification is [protocol.md §13](protocol.md).

- **Zero** (sensor offset with *no load*): disconnect the load, **[Check sensor]**, read the result, **[Set zero]**. The meter then subtracts
  the offset from every reading, so the history, energy and limits are already corrected.
- **Tare** (a *connected* load that should count as 0): connect it, **[Check sensor]**, **[Set tare]**. The tare is the measured load minus
  the zero. Tared values are shown with the *Raw | Tared* switch, in the history, CSV (extra `…_tared` columns) and cost, and the meter
  uses them for its limits when *Use tared values for limits* is on.
- **[Check sensor]** needs the meter online. It takes **at least 10 s (10 readings)**; the page shows "7 / 10 readings", then mean / min /
  max of current, power and voltage and either *Good: would set …* or why not: not stable, too few readings, a load seems to be connected
  (zero needs ≤ 1 A and ≤ 100 W), no load measured (tare), or the check is older than 10 minutes. **Set** is only enabled for a good check of
  the same kind. **[Clear]** removes the value. Every change raises the config version and is sent to the meter at once.
- Events *zero_set*, *tare_set* and *calibration_cleared* are recorded in the meter's event list. Calibration (known loads) is designed in
  protocol.md §13.7 and not implemented yet.

## Firmware list, Flash (OTA) and USB flash

*Firmware* page: **List of firmware** (checkbox, title, info, type, version, size, by, [Flash] [Info] [Edit]). Admin: **Add New Firmware**
(title, info, description, version, zip), Edit (text; optionally a new zip of the same firmware type), *Delete selected* (bulk). Manager: Info and
Flash only. The zip is the Arduino IDE export (`*.ino.bin`, bootloader, partitions, `boot_app0`, `flash_args`); the server reads type, version
and hardware from the tag inside the image and refuses a file without it. Needs `client_max_body_size` of about 80 MB on a reverse proxy.

**Flash** has two ways:
- **Over the air**: *Search & select meter*, then *Firmware Info* and *Meter Info* side by side (type, version, hardware, free update space,
  partition layout). Red verdicts block (offline, other type, other layout, too big, a firmware without OTA support = flash once by USB);
  warnings (older version, running session, relays off) need the check box. *Upload the Firmware to Meter* starts it; the page shows
  downloading / checking / restarting / done, or failed / rolled back (the meter went back to its old version).
- **By USB (browser)**: Connect (port picker) → chip, MAC and flash size are read and checked → *Update* (keeps the settings) or *Erase all
  flash first* (type the device ID of the meter, or the chip MAC of a new board) → Flash with a progress bar and log. Works in Chrome / Edge /
  Opera on a computer over **https://** or `localhost`; otherwise the tab says why it is off. If the board does not enter download mode:
  hold BOOT, press RESET.

Notification **Meter restart** (on by default): hardware profile restart pending, firmware update restarting / updated / failed / rolled back.

## AUX relay events: up to 3 conditions (OR)

In *Configuration → AUX relays*, mode **Event** has one line "While **any** of these is true the relay is ON / OFF (otherwise the opposite)" and up to
**3 conditions** joined by **— OR —** (e.g. *No Wi-Fi* OR *Tamper* OR *Session ended*). *Add condition (OR)* shows the next line; untick a line to remove it.
Each condition has its own source, channel / sensor, comparison and value (hysteresis per condition). The meter evaluates them itself (also offline).
The Info page shows them as `Event: ON while No Wi-Fi OR Tamper OR the session is ended`. Older single events are converted to one condition.

After **Save** (any tab) the page returns to the Configuration page (the same tab is reopened), not to the meter Info page.

**rbAmp modules found** (Update Device I/O): when the page opens (and with *Read now* / *Scan Address*) the meter scans the bus and sends, for every rbAmp it finds, its identity (variant UI1 … UI7 / I1 … I3, number of current inputs, voltage input, version, uptime, result valid / error) and one live reading: voltage and Hz, and A / W / PF of every input. The rbAmp board chips show the variant next to *found*. A found module that is not in the profile is marked *not in the profile*. (`res.modules` of the `i2c_scan` ack, protocol §12.)
