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

Login: rate limited to 5 tries per minute per IP. Changing the password ends other sessions.

## Pages

### `/setup` (first run only)
Username, Password, Confirm Password, Phone, Email (all required) + Setup token if `SETUP_TOKEN` is set → creates the admin → goes to the dashboard.

### Dashboard `/`
- A grid of **meter cards**: name, info, online dot, relay state, live W, session kWh used / max (progress bar), session status (active / quota / expired / tripped), last seen.
- Cards update live (the browser gets updates over a UI WebSocket or SSE).
- Left nav: Dashboard, **[Add New Meter]** (admin), Settings, Logout.

### Add New Meter (admin)
QR + Server address + Port + Secure + Pairing code + 10-min countdown + [Regenerate]. Redirects to Configure when paired. Shows a warning if *Settings → Server* is not set yet.

### Configure Meter Device (admin)
Shows a **Synced / Pending sync** badge.

| Section | Fields |
|---------|--------|
| General | Device ID (read-only), Nick Name, Info, Send data every [10 s – 1 h] |
| Actions | [Renew Now] · [Set to Default] (resets config only, keeps name/info/history) · [Remove Device] (confirm dialog with [ ] Also delete history; warns that the meter will **factory reset**) |
| Limits | Max kWh per session (0 = unlimited) · Session reset: ( ) None ( ) Duration [D][H][M] ( ) Daily at [HH:MM] (display timezone) · [ ] Auto renew · Max A to switch relay off (0 = off) · [ ] Relay off if device offline for [X] min |
| Notifications | [ ] kWh exceeded · [ ] Session renewed · [ ] Max A · [ ] Panel open · [ ] No voltage · [ ] Disconnected for [X] min · [ ] Reconnected |

[Save] → stores config v+1 → sends to device → waits up to 15 s for `cfg_ack`:
- ack ok → "Saved and confirmed by device", close page → Meter Info.
- rejected → show the device's error, keep the form open.
- device offline / timeout → "Saved — will apply when the device reconnects" (Pending sync).

### Meter Info (view-only; admin and viewers)
- Device ID, Nick Name, Info, kWh per session, next session reset (display TZ), auto renew yes/no, last online, firmware.
- Latest data: V, A, W, PF, Hz, session kWh used / remaining.
- Admin-only buttons: Relay ON/OFF, [Renew Now], [Reset Trip], [Send Data Now], [Configure].
- History chart (V / A / W / kWh) with range: 1 h, 1 day, 1 week, 1 month, all time, custom; stats: min / max / avg.
- [Export CSV] exports exactly the range currently shown (admin and viewers).
- Recent events list (sessions, trips, tamper, AC loss, connect/disconnect).

### Settings
| Tab | Content |
|-----|---------|
| Account | Change username, password, phone, email (current password required) |
| Server (admin) | Public address, port, TLS · Display timezone (IANA list, shows its UTC offset) · Raw data retention |
| Users (admin) | List / add / disable / delete **viewer** accounts, reset viewer password |
| Notifications (admin) | In-app (always on) · Telegram: bot token, chat ID, [Send test] — *future* |
