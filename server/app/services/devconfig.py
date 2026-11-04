"""Meter configuration: defaults, form parsing/validation, and the document sent to the device.

The server keeps a UI-level config in Device.cfg. `to_device()` turns it into the protocol
config (docs/protocol.md §3), e.g. converting the daily reset time to a UTC minute-of-day.
"""
from . import settings

NOTIFY_TYPES = [
    ("kwh_exceeded", "Device exceeded kWh"),
    ("session_renew", "Session renewed"),
    ("max_amp", "Max amp (trip)"),
    ("panel_open", "Panel open"),
    ("no_voltage", "No voltage"),
    ("disconnected", "Device disconnected for"),
    ("reconnected", "Device reconnected"),
]
NOTIFY_LABELS = dict(NOTIFY_TYPES)
DISCONNECTED_DEFAULT_MIN = 5

REPORT_MIN, REPORT_MAX = 10, 3600

# When the kWh limit is reached (per channel):
KWH_ACTIONS = {
    "end": "Turn power off and end the session now, even if time is left",
    "hold": "Turn power off, but end the session only when the time limit is reached",
    "warn": "Only warn, keep the power on",
}


def kwh_action(ch_cfg):
    a = ch_cfg.get("kwh_action")
    if a in KWH_ACTIONS:
        return a
    return "end" if ch_cfg.get("kwh_end", True) else "warn"  # config saved by v0.2
SESSION_MODES = ("none", "duration", "daily")


def channel_count(device):
    try:
        return max(1, min(8, int((device.caps or {}).get("channels", 1))))
    except (TypeError, ValueError):
        return 1


def amp_rating(device):
    try:
        return float((device.caps or {}).get("amp_rating", 100))
    except (TypeError, ValueError):
        return 100.0


def default_cfg(n_channels=1):
    return {
        "report_s": 60,
        "offline_off": False,
        "offline_off_min": 3,
        "sess_mode": "none",
        "dur_min": 1440,
        "daily_at": "00:00",
        "auto": False,
        "ch": [{"kwh_max": 0.0, "kwh_action": "end", "amp_max": 0.0} for _ in range(n_channels)],
    }


def _num(form, key, cast, lo, hi, label, errors, default=0):
    raw = (form.get(key) or "").strip()
    if raw == "":
        return default
    try:
        value = cast(raw)
    except ValueError:
        errors.append(f"{label}: not a number")
        return default
    if not lo <= value <= hi:
        errors.append(f"{label}: must be between {lo} and {hi}")
        return default
    return value


def parse_form(form, device):
    """Return (cfg, errors) from the Configure form."""
    errors = []
    cfg = default_cfg(channel_count(device))
    cfg["report_s"] = _num(form, "report_s", int, REPORT_MIN, REPORT_MAX, "Send data every (s)", errors, 60)
    cfg["offline_off"] = form.get("offline_off") == "on"
    cfg["offline_off_min"] = _num(form, "offline_off_min", int, 1, 1440, "Offline minutes", errors, 3)

    mode = form.get("sess_mode", "none")
    cfg["sess_mode"] = mode if mode in SESSION_MODES else "none"
    days = _num(form, "dur_d", int, 0, 365, "Days", errors)
    hours = _num(form, "dur_h", int, 0, 23, "Hours", errors)
    mins = _num(form, "dur_m", int, 0, 59, "Minutes", errors)
    dur = days * 1440 + hours * 60 + mins
    if cfg["sess_mode"] == "duration" and dur < 1:
        errors.append("Session duration must be at least 1 minute")
    cfg["dur_min"] = max(1, dur)
    daily = (form.get("daily_at") or "00:00").strip()
    try:
        h, m = daily.split(":")
        assert 0 <= int(h) <= 23 and 0 <= int(m) <= 59
        cfg["daily_at"] = f"{int(h):02d}:{int(m):02d}"
    except (ValueError, AssertionError):
        errors.append("Daily reset time must be HH:MM")
    cfg["auto"] = form.get("auto") == "on"

    rating = amp_rating(device)
    for i, ch in enumerate(cfg["ch"]):
        ch["kwh_max"] = _num(form, f"kwh_max_{i}", float, 0, 100000, f"CH{i+1} max kWh", errors, 0.0)
        action = form.get(f"kwh_action_{i}", "end")
        ch["kwh_action"] = action if action in KWH_ACTIONS else "end"
        ch["amp_max"] = _num(form, f"amp_max_{i}", float, 0, rating, f"CH{i+1} max A", errors, 0.0)
    return cfg, errors


def to_device(cfg, ver):
    sess_mode = cfg.get("sess_mode", "none")
    sess = {"mode": sess_mode, "auto": bool(cfg.get("auto"))}
    if sess_mode == "daily":
        sess["daily_utc_min"] = settings.local_hhmm_to_utc_minute(cfg.get("daily_at", "00:00"))
    if sess_mode == "duration":
        sess["dur_min"] = int(cfg.get("dur_min", 1440))
    return {
        "ver": ver,
        "cfg": {
            "report_s": int(cfg.get("report_s", 60)),
            "offline_off_min": int(cfg.get("offline_off_min", 3)) if cfg.get("offline_off") else 0,
            "sess": sess,
            "ch": [
                {"i": i, "kwh_max": float(c.get("kwh_max", 0)), "kwh_action": kwh_action(c),
                 # for meters on firmware 0.1.x that only know kwh_end ("hold" acts like "end" there)
                 "kwh_end": kwh_action(c) != "warn",
                 "amp_max": float(c.get("amp_max", 0))}
                for i, c in enumerate(cfg.get("ch", []))
            ],
        },
    }


def describe_session(cfg):
    mode = cfg.get("sess_mode", "none")
    if mode == "daily":
        return f"Every day at {cfg.get('daily_at', '00:00')}"
    if mode == "duration":
        d, rest = divmod(int(cfg.get("dur_min", 0)), 1440)
        h, m = divmod(rest, 60)
        parts = [f"{d}d" if d else "", f"{h}h" if h else "", f"{m}m" if m else ""]
        return "Every " + " ".join(p for p in parts if p) + " from session start"
    return "No time limit"
