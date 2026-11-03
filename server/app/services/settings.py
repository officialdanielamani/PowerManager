"""Key/value settings with a small in-process cache (single worker)."""
import threading
from datetime import datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError, available_timezones

from ..extensions import db
from ..models import Setting

DEFAULTS = {
    "server_address": "",
    "server_port": "",
    "server_tls": "1",
    "timezone": "UTC",
    "raw_retention_days": "90",
    "telegram_enabled": "0",
    "telegram_bot_token": "",
    "telegram_chat_id": "",
}

_cache = {}
_lock = threading.Lock()


def get(key):
    with _lock:
        if key in _cache:
            return _cache[key]
    row = db.session.get(Setting, key)
    value = row.value if row else DEFAULTS.get(key, "")
    with _lock:
        _cache[key] = value
    return value


def get_int(key, default=0):
    try:
        return int(get(key))
    except (TypeError, ValueError):
        return default


def set_many(values):
    for key, value in values.items():
        row = db.session.get(Setting, key)
        if row is None:
            row = Setting(key=key)
            db.session.add(row)
        row.value = str(value)
    db.session.commit()
    with _lock:
        for key, value in values.items():
            _cache[key] = str(value)


def clear_cache():
    with _lock:
        _cache.clear()


# ---- time zone helpers -------------------------------------------------

_ZONES = None


def zone_names():
    global _ZONES
    if _ZONES is None:
        _ZONES = sorted(z for z in available_timezones() if "/" in z or z == "UTC")
    return _ZONES


def tz():
    try:
        return ZoneInfo(get("timezone") or "UTC")
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo("UTC")


def utc_offset_str(zone_name=None, at=None):
    zone = ZoneInfo(zone_name) if zone_name else tz()
    dt = datetime.now(timezone.utc) if at is None else datetime.fromtimestamp(at, timezone.utc)
    off = dt.astimezone(zone).utcoffset()
    minutes = int(off.total_seconds() // 60)
    sign = "+" if minutes >= 0 else "-"
    minutes = abs(minutes)
    return f"UTC{sign}{minutes // 60:02d}:{minutes % 60:02d}"


def fmt_ts(ts, with_seconds=True):
    if not ts:
        return "–"
    dt = datetime.fromtimestamp(int(ts), timezone.utc).astimezone(tz())
    return dt.strftime("%Y-%m-%d %H:%M:%S" if with_seconds else "%Y-%m-%d %H:%M")


def local_to_epoch(text):
    """'YYYY-MM-DDTHH:MM' (display time zone) -> epoch seconds, or None."""
    try:
        dt = datetime.strptime(text, "%Y-%m-%dT%H:%M")
    except (TypeError, ValueError):
        return None
    return int(dt.replace(tzinfo=tz()).timestamp())


def local_hhmm_to_utc_minute(hhmm):
    """Daily reset time in the display zone -> UTC minute-of-day, using today's offset."""
    h, m = (int(x) for x in hhmm.split(":"))
    off = datetime.now(timezone.utc).astimezone(tz()).utcoffset()
    return int((h * 60 + m - off.total_seconds() // 60) % 1440)


def public_server(request=None):
    """(host, port, tls) used in the pairing QR. Falls back to the browser's host."""
    host = get("server_address").strip()
    port = get("server_port").strip()
    tls = get("server_tls") == "1"
    configured = bool(host)
    if not host and request is not None:
        host = request.host.split(":")[0]
        if ":" in request.host:
            port = request.host.split(":")[1]
        tls = request.scheme == "https"
    port = int(port) if port else (443 if tls else 80)
    return host, port, tls, configured
