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
- A grid of **meter cards**: name, info, online dot, relay state, live W, session kWh used / max (progress bar), session status (active / quota / expired / tripped), last seen.
- Cards update live (the browser gets updates over a UI WebSocket or SSE).
- Left nav: Dashboard, **[Add New Meter]** (admin), Notifications, **Firmware** (admin; placeholder for OTA updates), Settings, Logout.
- View-only users see only the meters linked to them (or all, if *View all meters* is set).

### Add New Meter (admin)
QR + Server address + Port + Secure + Pairing code + 10-min countdown + [Regenerate]. Redirects to Configure when paired. Shows a warning if *Settings → Server* is not set yet.

### Configure Meter Device (admin)
Shows a **Synced / Pending sync** badge.

| Section | Fields |
|---------|--------|
| General | Device ID (read-only), Nick Name, Info, Send data every [10 s – 1 h] |
| Actions | [Renew Now] · [Set to Default] (resets config only, keeps name/info/history) · [Remove Device] (confirm dialog with [ ] Also delete history; warns that the meter will **factory reset**) |
| Limits | Max kWh per session (0 = unlimited) + **When the kWh limit is reached**: (•) power off and end the session now, even if time is left (default) · ( ) power off, end the session only at the time limit · ( ) only warn, keep power on · Session reset: ( ) None ( ) Duration [D][H][M] ( ) Daily at [HH:MM] (display timezone) · [ ] Auto renew · Max A to switch relay off (0 = off) · [ ] Relay off if device offline for [X] min |
| Link this meter to users | Checkbox per view-only user (server-only, never sent to the meter) |

Notification options are **not** here any more: see *Notification Options* below.

[Save] → stores config v+1 → sends to device → waits up to 15 s for `cfg_ack`:
- ack ok → "Saved and confirmed by device", close page → Meter Info.
- rejected → show the device's error, keep the form open.
- device offline / timeout → "Saved — will apply when the device reconnects" (Pending sync).

Each meter has three tabs: **Info** (everyone with access), **Notification Options** (everyone with access, per account), **Configuration** (admin).

### Notification Options (admin and linked viewers, per account)
Switches: kWh exceeded · Session renewed · Max amp · Panel open · No voltage · Disconnected for [X] min · Reconnected. They apply only to the logged-in account.

### Meter Info (view-only; admin and viewers)
- Device ID, Nick Name, Info, kWh per session, next session reset (display TZ), **session cost** (if a price is set), **session started, time elapsed, time left** (live, ticks every second; "Session ended" once a limit ended it), auto renew yes/no, last online, firmware.
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
- Session history (start, end, duration, kWh used, **cost**, end reason, **[CSV]** = power data of that session); the history stats show the cost of the selected range as a second tab next to Historical Events.

### Settings
| Tab | Content |
|-----|---------|
| Account | Change username, password, phone, email (current password required) · My Telegram chat ID + [Send test] |
| Server (admin) | Public address, port, TLS · Display timezone (IANA list, shows its UTC offset) · Raw data retention · **Electricity price**: [Currency][Price] / kWh (e.g. MYR 0.21) |
| Users (admin) | View-only accounts: select + Enable/Disable/Unlock selected, Enable all / Disable all · per user: meters they can see (or *View all meters*), reset password (also unlocks), enable/disable, delete · Add account (with meters) · Login protection (max failed logins, lock minutes) |
| Notifications (admin) | Telegram on/off + bot token (each user adds their own chat ID) |

### Firmware (admin)
Placeholder page for future OTA updates. The meter's own page has a greyed-out **Check OTA Update** button.
