"""Meter configuration: defaults, form parsing/validation, and the document sent to the device.

The server keeps a UI-level config in Device.cfg. `to_device()` turns it into the protocol
config (docs/protocol.md §3), e.g. converting the daily reset time to a UTC minute-of-day.
"""
from . import auxcfg, settings

NOTIFY_TYPES = [
    ("kwh_exceeded", "Device exceeded kWh"),
    ("voltage_range", "Voltage out of range"),
    ("session_renew", "Session renewed"),
    ("max_amp", "Max amp (trip)"),
    ("panel_open", "Panel open"),
    ("no_voltage", "No voltage"),
    ("sensor_error", "Sensor error (no reading)"),
    ("temp_high", "Temperature too high"),
    ("disconnected", "Device disconnected for"),
    ("reconnected", "Device reconnected"),
    ("meter_status", "Meter status (disabled / enabled / unsynced / synced / removed)"),
]
# Types that are on for every account until the account switches them off.
NOTIFY_DEFAULT_ON = {"meter_status", "temp_high"}
NOTIFY_LABELS = dict(NOTIFY_TYPES)
DISCONNECTED_DEFAULT_MIN = 5

REPORT_MIN, REPORT_MAX = 10, 3600   # send to the server every ... s
READ_MIN = 1                        # record a reading every ... s (read <= send)
DRDY_SEND_MAX, DRDY_SEND_DEFAULT = 90, 15  # DRDY data mode (every ~100 ms window recorded): send every 1-90 s

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
SENSOR_TIMEOUT_DEFAULT, SENSOR_TIMEOUT_MAX = 10, 3600


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
        "read_s": 60,
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
        "tare_a": 0.0,             # own consumption (ESP32 + SSR) on the measured line
        "tare_w": 0.0,
        "tare_limits": True,       # the meter uses tared values for the kWh limit and max current
        "sensor_to_s": SENSOR_TIMEOUT_DEFAULT,  # no valid reading for this long = sensor error; 0 = off
        "sensor_action": DEFAULT_PROTECT_ACTION,
        "temp_max": 0.0,           # over-temperature (any DS18B20 sensor); 0 = off
        "temp_action": DEFAULT_PROTECT_ACTION,
    }


MC_MODES = ("shared", "independent")


def hw_signature(caps):
    """Meter type as reported by its firmware: AC / DC, single-channel PZEM or rbAmp with N channels.
    A change means different hardware on the same ESP32 (same Device ID)."""
    caps = caps or {}
    kind = "dc" if caps.get("kind") == "dc" else "ac"
    if caps.get("mc"):
        try:
            n = int(caps["channels"])
        except (KeyError, TypeError, ValueError):
            return None  # module not found yet: channel count unknown, do not decide anything
        return f"{kind}-mc{n}"
    return f"{kind}-1"


def hw_label(sig):
    """'AC (PZEM-004T)', 'DC (PZEM-017)', 'AC multi-channel, 2 ch' ..."""
    sig = sig or "ac-1"
    if "-mc" in sig:
        return f"{sig.split('-')[0].upper()} multi-channel, {sig.split('-mc')[1]} ch"
    return "DC (PZEM-017)" if sig.startswith("dc") else "AC (PZEM-004T)"


def _mc_channel_default():
    return {"name": "", "amp_max": 0.0, "kwh_max": 0.0, "kwh_action": DEFAULT_KWH_ACTION,
            "sess_mode": "none", "dur_min": 1440, "daily_at": "00:00", "auto": False}


def default_for(device):
    """Default config of a meter (multi-channel meters get the shared / independent fields)."""
    n = channel_count(device)
    cfg = default_cfg(n)
    if device.is_mc:
        cfg.update({"mode": "shared", "drdy": False, "kwh_max": 0.0, "kwh_action": DEFAULT_KWH_ACTION})
        cfg["ch"] = [_mc_channel_default() for _ in range(n)]
        for k in ("tare_a", "tare_w", "tare_limits"):  # the clamps measure only the load
            cfg.pop(k, None)
    if aux_count(device):
        cfg["aux"] = auxcfg.fit([], aux_count(device))
    return cfg


def aux_count(device):
    """AUX relays of a meter: relay outputs no measured channel uses (caps.aux, both firmwares)."""
    try:
        return min(16, max(0, int((device.caps or {}).get("aux", 0) or 0)))
    except (TypeError, ValueError):
        return 0


def fit_aux(device):
    """The meter reported a different number of AUX relays: pad / trim the AUX config. True if changed."""
    n = aux_count(device)
    cfg = dict(device.cfg or default_for(device))
    have = cfg.get("aux") or []
    if len(have) == n and all("mode" in a for a in have):
        return False
    cfg["aux"] = auxcfg.fit(have, n)
    if not n:
        cfg.pop("aux", None)
    device.cfg = cfg
    return True


def fit_channels(device):
    """A multi-channel meter reported a different channel count: pad / trim the per-channel config.
    Returns True if the config changed."""
    if not device.is_mc:
        return False
    n = channel_count(device)
    cfg = dict(device.cfg or default_for(device))
    chans = list(cfg.get("ch") or [])
    if len(chans) == n and "mode" in cfg:
        return fit_aux(device)
    if "mode" not in cfg:  # config made before the meter said it is multi-channel
        base = default_for(device)
        base.update({k: v for k, v in cfg.items() if k != "ch"})
        cfg = base
        chans = [dict(_mc_channel_default(), **{k: c.get(k) for k in ("amp_max", "kwh_max", "kwh_action") if k in c})
                 for c in chans]
    chans = (chans + [_mc_channel_default() for _ in range(n)])[:n]
    cfg["ch"] = chans
    cfg["aux"] = auxcfg.fit(cfg.get("aux"), aux_count(device))
    device.cfg = cfg
    return True


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


def _parse_sess(form, sfx, errors, label):
    """Session time limit fields (shared: no suffix, independent: _<i>)."""
    out = {}
    mode = form.get("sess_mode" + sfx, "none")
    out["sess_mode"] = mode if mode in SESSION_MODES else "none"
    days = _num(form, "dur_d" + sfx, int, 0, 365, f"{label}Days", errors)
    hours = _num(form, "dur_h" + sfx, int, 0, 23, f"{label}Hours", errors)
    mins = _num(form, "dur_m" + sfx, int, 0, 59, f"{label}Minutes", errors)
    dur = days * 1440 + hours * 60 + mins
    if out["sess_mode"] == "duration" and dur < 1:
        errors.append(f"{label}Session duration must be at least 1 minute")
    out["dur_min"] = max(1, dur)
    daily = (form.get("daily_at" + sfx) or "00:00").strip()
    try:
        h, m = daily.split(":")
        assert 0 <= int(h) <= 23 and 0 <= int(m) <= 59
        out["daily_at"] = f"{int(h):02d}:{int(m):02d}"
    except (ValueError, AssertionError):
        errors.append(f"{label}Daily reset time must be HH:MM")
        out["daily_at"] = "00:00"
    out["auto"] = form.get("auto" + sfx) == "on"
    return out


def _parse_mc(form, device, cfg, errors):
    """Multi-channel fields: mode, DRDY, shared total limit, per-channel names / limits / sessions."""
    mode = form.get("mode", "shared")
    cfg["mode"] = mode if mode in MC_MODES else "shared"
    cfg["drdy"] = form.get("drdy") == "on" and bool((device.caps or {}).get("drdy", True))
    cfg["kwh_max"] = _num(form, "kwh_max", float, 0, 100000, "Max kWh (all channels)", errors, 0.0)
    a = form.get("kwh_action", DEFAULT_KWH_ACTION)
    cfg["kwh_action"] = a if a in KWH_ACTIONS else DEFAULT_KWH_ACTION
    rating = amp_rating(device)
    chans = []
    for i in range(channel_count(device)):
        label = f"CH{i + 1}: "
        c = _mc_channel_default()
        c["name"] = (form.get(f"name_{i}") or "").strip()[:20]
        if any(ch in c["name"] for ch in "<>\"'&"):
            errors.append(f"{label}name: letters, numbers and spaces only")
            c["name"] = ""
        c["amp_max"] = _num(form, f"amp_max_{i}", float, 0, rating, f"{label}max A", errors, 0.0)
        c["kwh_max"] = _num(form, f"kwh_max_{i}", float, 0, 100000, f"{label}max kWh", errors, 0.0)
        a = form.get(f"kwh_action_{i}", DEFAULT_KWH_ACTION)
        c["kwh_action"] = a if a in KWH_ACTIONS else DEFAULT_KWH_ACTION
        c.update(_parse_sess(form, f"_{i}", errors, label))
        chans.append(c)
    cfg["ch"] = chans
    for k in ("tare_a", "tare_w", "tare_limits"):
        cfg.pop(k, None)


def parse_form(form, device):
    """Return (cfg, errors) from the Configure form."""
    errors = []
    cfg = default_for(device)
    drdy = device.is_mc and form.get("drdy") == "on" and bool((device.caps or {}).get("drdy", (device.caps or {}).get("rdy", True)))
    if drdy:
        # DRDY data mode: every measurement window is recorded (no read interval); send every 1-90 s.
        cfg["report_s"] = _num(form, "report_s_drdy", int, 1, DRDY_SEND_MAX, "Send every (s)", errors, DRDY_SEND_DEFAULT)
        cfg["read_s"] = _num(form, "read_s", int, READ_MIN, REPORT_MAX, "Read data every (s)", [], 60)  # kept for Normal
    else:
        cfg["report_s"] = _num(form, "report_s", int, REPORT_MIN, REPORT_MAX, "Send data every (s)", errors, 60)
        cfg["read_s"] = _num(form, "read_s", int, READ_MIN, REPORT_MAX, "Read data every (s)", errors, cfg["report_s"])
        if cfg["read_s"] > cfg["report_s"]:
            errors.append("Read data every: must not be longer than Send every")
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
    for i, ch in enumerate([] if device.is_mc else cfg["ch"]):
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
    cfg["tare_a"] = _num(form, "tare_a", float, 0, 5, "Tare current", errors, 0.0)
    cfg["tare_w"] = _num(form, "tare_w", float, 0, 500, "Tare power", errors, 0.0)
    cfg["tare_limits"] = form.get("tare_limits") == "on"
    cfg["tamper_protect"] = form.get("tamper_protect") == "on"
    a = form.get("tamper_action", DEFAULT_PROTECT_ACTION)
    cfg["tamper_action"] = a if a in PROTECT_ACTIONS else DEFAULT_PROTECT_ACTION
    cfg["sensor_to_s"] = _num(form, "sensor_to_s", int, 0, SENSOR_TIMEOUT_MAX, "Sensor timeout (s)", errors,
                              SENSOR_TIMEOUT_DEFAULT)
    if 0 < cfg["sensor_to_s"] < 3:
        errors.append("Sensor timeout: at least 3 s (or 0 = off)")
    a = form.get("sensor_action", DEFAULT_PROTECT_ACTION)
    cfg["sensor_action"] = a if a in PROTECT_ACTIONS else DEFAULT_PROTECT_ACTION
    cfg["temp_max"] = _num(form, "temp_max", float, 0, 125, "Max temperature", errors, 0.0)
    a = form.get("temp_action", DEFAULT_PROTECT_ACTION)
    cfg["temp_action"] = a if a in PROTECT_ACTIONS else DEFAULT_PROTECT_ACTION
    if device.is_mc:
        _parse_mc(form, device, cfg, errors)
    if aux_count(device):
        cfg["aux"] = auxcfg.parse_form(form, aux_count(device), errors)
    return cfg, errors


def _sess_doc(c):
    sess_mode = c.get("sess_mode", "none")
    sess = {"mode": sess_mode, "auto": bool(c.get("auto"))}
    if sess_mode == "daily":
        sess["daily_utc_min"] = settings.local_hhmm_to_utc_minute(c.get("daily_at", "00:00"))
    if sess_mode == "duration":
        sess["dur_min"] = int(c.get("dur_min", 1440))
    return sess


def to_device(cfg, ver):
    if "mode" in cfg:  # multi-channel meter (docs/multichannel.md)
        return {"ver": ver, "cfg": {
            "report_s": int(cfg.get("report_s", 60)),
            "read_s": int(cfg.get("read_s", cfg.get("report_s", 60))),
            "read_ms": int(cfg.get("read_s", cfg.get("report_s", 60))) * 1000,
            "drdy": bool(cfg.get("drdy", cfg.get("rdy"))),
            "offline_off_min": int(cfg.get("offline_off_min", 3)) if cfg.get("offline_off") else 0,
            "mode": cfg.get("mode", "shared"),
            "sess": _sess_doc(cfg),
            "kwh_max": float(cfg.get("kwh_max", 0) or 0),
            "kwh_action": cfg.get("kwh_action", DEFAULT_KWH_ACTION),
            "volt": {"min": float(cfg.get("volt_min", 0) or 0), "max": float(cfg.get("volt_max", 0) or 0),
                     "action": cfg.get("volt_action", DEFAULT_PROTECT_ACTION)},
            "tamper": {"protect": bool(cfg.get("tamper_protect")),
                       "action": cfg.get("tamper_action", DEFAULT_PROTECT_ACTION)},
            "sensor": {"timeout_s": int(cfg.get("sensor_to_s", SENSOR_TIMEOUT_DEFAULT)),
                       "action": cfg.get("sensor_action", DEFAULT_PROTECT_ACTION)},
            "temp": {"max": float(cfg.get("temp_max", 0) or 0), "action": cfg.get("temp_action", DEFAULT_PROTECT_ACTION)},
            "ch": [{"i": i, "name": c.get("name", ""), "amp_max": float(c.get("amp_max", 0) or 0),
                    "kwh_max": float(c.get("kwh_max", 0) or 0), "kwh_action": c.get("kwh_action", DEFAULT_KWH_ACTION),
                    "sess": _sess_doc(c)} for i, c in enumerate(cfg.get("ch", []))],
            "aux": auxcfg.to_device(cfg.get("aux")),
            "tz_min": auxcfg.tz_minutes(),
        }}
    sess = _sess_doc(cfg)
    return {
        "ver": ver,
        "cfg": {
            "report_s": int(cfg.get("report_s", 60)),
            "read_s": int(cfg.get("read_s", cfg.get("report_s", 60))),
            "offline_off_min": int(cfg.get("offline_off_min", 3)) if cfg.get("offline_off") else 0,
            "sess": sess,
            "volt": {"min": float(cfg.get("volt_min", 0) or 0), "max": float(cfg.get("volt_max", 0) or 0),
                     "action": cfg.get("volt_action", DEFAULT_PROTECT_ACTION)},
            "tare": {"a": float(cfg.get("tare_a", 0) or 0), "w": float(cfg.get("tare_w", 0) or 0),
                     "limits": bool(cfg.get("tare_limits", True))},
            "tamper": {"protect": bool(cfg.get("tamper_protect")),
                       "action": cfg.get("tamper_action", DEFAULT_PROTECT_ACTION)},
            "sensor": {"timeout_s": int(cfg.get("sensor_to_s", SENSOR_TIMEOUT_DEFAULT)),
                       "action": cfg.get("sensor_action", DEFAULT_PROTECT_ACTION)},
            "temp": {"max": float(cfg.get("temp_max", 0) or 0), "action": cfg.get("temp_action", DEFAULT_PROTECT_ACTION)},
            "ch": [
                {"i": i, "kwh_max": float(c.get("kwh_max", 0)), "kwh_action": kwh_action(c),
                 # for meters on firmware 0.1.x that only know kwh_end ("hold" acts like "end" there)
                 "kwh_end": kwh_action(c) != "warn",
                 "amp_max": float(c.get("amp_max", 0))}
                for i, c in enumerate(cfg.get("ch", []))
            ],
            "aux": auxcfg.to_device(cfg.get("aux")),
            "tz_min": auxcfg.tz_minutes(),
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
