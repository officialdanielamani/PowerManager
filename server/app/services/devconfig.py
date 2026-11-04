"""Meter configuration: defaults, form parsing/validation, and the document sent to the device.

The server keeps a UI-level config in Device.cfg. `to_device()` turns it into the protocol
config (docs/protocol.md §3), e.g. converting the daily reset time to a UTC minute-of-day.
"""
from . import settings

NOTIFY_TYPES = [
    ("kwh_exceeded", "Device exceeded kWh"),
    ("voltage_range", "Voltage out of range"),
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

# What a meter does when a limit / protection triggers. Same three choices everywhere.
#   run  - session runs as usual (only an event + notification)
#   hold - relay off, the session continues (kWh: until the session ends or is renewed;
#          voltage / tamper: until the condition clears)
#   end  - end the session now (relay off until renewed / auto renew at the next scheduled time)
KWH_ACTIONS = {
    "warn": "Session run as usual",
    "hold": "Off relay, but session continue",
    "end": "End session",
}
PROTECT_ACTIONS = {
    "run": "Session run as usual",
    "hold": "Off relay, but session continue",
    "end": "End session",
}
DEFAULT_KWH_ACTION = "hold"
DEFAULT_PROTECT_ACTION = "hold"
VOLT_MIN, VOLT_MAX = 0, 400


def kwh_action(ch_cfg):
    a = ch_cfg.get("kwh_action")
    if a in KWH_ACTIONS:
        return a
    if "kwh_end" in ch_cfg:  # config saved by v0.2
        return "end" if ch_cfg["kwh_end"] else "warn"
    return DEFAULT_KWH_ACTION


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
        "ch": [{"kwh_max": 0.0, "kwh_action": DEFAULT_KWH_ACTION, "amp_max": 0.0} for _ in range(n_channels)],
        "volt_min": 0.0,           # 0 = no lower limit
        "volt_max": 0.0,           # 0 = no upper limit
        "volt_action": DEFAULT_PROTECT_ACTION,
        "tamper_protect": False,
        "tamper_action": DEFAULT_PROTECT_ACTION,
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
        action = form.get(f"kwh_action_{i}", DEFAULT_KWH_ACTION)
        ch["kwh_action"] = action if action in KWH_ACTIONS else DEFAULT_KWH_ACTION
        ch["amp_max"] = _num(form, f"amp_max_{i}", float, 0, rating, f"CH{i+1} max A", errors, 0.0)

    cfg["volt_min"] = _num(form, "volt_min", float, VOLT_MIN, VOLT_MAX, "Min voltage", errors, 0.0)
    cfg["volt_max"] = _num(form, "volt_max", float, VOLT_MIN, VOLT_MAX, "Max voltage", errors, 0.0)
    if cfg["volt_min"] and cfg["volt_max"] and cfg["volt_min"] >= cfg["volt_max"]:
        errors.append("Operating voltage: Min must be lower than Max")
    a = form.get("volt_action", DEFAULT_PROTECT_ACTION)
    cfg["volt_action"] = a if a in PROTECT_ACTIONS else DEFAULT_PROTECT_ACTION
    cfg["tamper_protect"] = form.get("tamper_protect") == "on"
    a = form.get("tamper_action", DEFAULT_PROTECT_ACTION)
    cfg["tamper_action"] = a if a in PROTECT_ACTIONS else DEFAULT_PROTECT_ACTION
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
            "volt": {"min": float(cfg.get("volt_min", 0) or 0), "max": float(cfg.get("volt_max", 0) or 0),
                     "action": cfg.get("volt_action", DEFAULT_PROTECT_ACTION)},
            "tamper": {"protect": bool(cfg.get("tamper_protect")),
                       "action": cfg.get("tamper_action", DEFAULT_PROTECT_ACTION)},
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
