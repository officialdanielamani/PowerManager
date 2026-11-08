"""AUX relay configuration: name + automation (manual / schedule / event) per AUX relay.

Stored in device.cfg["aux"] in display form (local "HH:MM", day list) and sent to the meter as minutes of
the local day + a day bit mask, with "tz_min" (UTC offset of the display time zone) so the meter evaluates
schedules in local time on its own, also offline. See docs/protocol.md (config "aux").
"""
from datetime import datetime, timezone

from . import settings

MODES = {"manual": "Manual (ON / OFF buttons)", "schedule": "Schedule", "event": "Event"}
SOURCES = {"temp": "Temperature", "hum": "Humidity", "amp": "Current (A)", "watt": "Power (W)",
           "kwh": "kWh balance", "session": "Session"}
UNITS = {"temp": "°C", "hum": "%RH", "amp": "A", "watt": "W", "kwh": "kWh"}
DAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
SCHED_MAX = 4
TOTAL = 255  # amp / watt of all channels together


def default():
    return {"name": "", "mode": "manual", "override": False, "else": "off", "sched": [], "event": None}


def fit(items, n):
    """Pad / trim a stored AUX list to n relays (older configs only had names)."""
    out = []
    for a in (list(items or []) + [None] * n)[:n]:
        out.append(dict(default(), **(a or {})))
    return out


def _hhmm(raw):
    try:
        h, m = str(raw or "").strip().split(":")[:2]
        h, m = int(h), int(m)
        return f"{h:02d}:{m:02d}" if 0 <= h <= 23 and 0 <= m <= 59 else None
    except ValueError:
        return None


def _minutes(hhmm):
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


def parse_form(form, n, errors):
    """AUX relays from the Configuration form (field names: aux_<field>_<j>, schedule rows aux_s<k>_...)."""
    out = []
    for j in range(n):
        label = f"AUX{j + 1}"
        a = default()
        name = (form.get(f"aux_name_{j}") or "").strip()[:20]
        if any(ch in name for ch in "<>\"'&"):
            errors.append(f"{label}: name: letters, numbers and spaces only")
            name = ""
        a["name"] = name
        mode = form.get(f"aux_mode_{j}", "manual")
        a["mode"] = mode if mode in MODES else "manual"
        a["override"] = form.get(f"aux_override_{j}") == "on"
        a["else"] = "on" if form.get(f"aux_else_{j}") == "on" else "off"
        for k in range(SCHED_MAX):
            if form.get(f"aux_s{k}_use_{j}") != "on":
                continue
            start, end = _hhmm(form.get(f"aux_s{k}_start_{j}")), _hhmm(form.get(f"aux_s{k}_end_{j}"))
            days = sorted({int(d) for d in form.getlist(f"aux_s{k}_days_{j}") if str(d).isdigit() and int(d) < 7})
            if not start or not end:
                errors.append(f"{label} schedule {k + 1}: enter a start and an end time")
                continue
            if not days:
                errors.append(f"{label} schedule {k + 1}: pick at least one day")
                continue
            a["sched"].append({"on": form.get(f"aux_s{k}_on_{j}", "on") == "on", "start": start, "end": end, "days": days})
        src = form.get(f"aux_ev_src_{j}", "temp")
        if src in SOURCES:
            idx = form.get(f"aux_ev_idx_{j}", "0")
            idx = int(idx) if str(idx).isdigit() and (int(idx) < 16 or int(idx) == TOTAL) else 0
            ev = {"src": src, "idx": idx, "on": form.get(f"aux_ev_on_{j}", "on") == "on"}
            if src == "session":
                ev["running"] = form.get(f"aux_ev_running_{j}", "running") == "running"
            else:
                ev["cmp"] = "under" if form.get(f"aux_ev_cmp_{j}") == "under" else "over"
                raw = (form.get(f"aux_ev_value_{j}") or "").strip()
                try:
                    ev["value"] = round(float(raw), 3)
                    if not -1000000 <= ev["value"] <= 1000000:
                        raise ValueError
                except ValueError:
                    ev["value"] = 0.0
                    if a["mode"] == "event":
                        errors.append(f"{label} event: enter a number for the value")
            a["event"] = ev
        if a["mode"] == "schedule" and not a["sched"]:
            errors.append(f"{label}: add at least one schedule (or choose Manual)")
        out.append(a)
    return out


def to_device(items):
    """The AUX list as the meter wants it."""
    out = []
    for j, a in enumerate(items or []):
        a = dict(default(), **(a or {}))
        doc = {"i": j, "name": a.get("name", ""), "mode": a["mode"], "override": bool(a.get("override")),
               "else": a.get("else", "off")}
        if a["mode"] == "schedule":
            doc["sched"] = [{"on": bool(s.get("on", True)), "start": _minutes(s["start"]), "end": _minutes(s["end"]),
                             "days": sum(1 << int(d) for d in s.get("days") or [])}
                            for s in (a.get("sched") or [])[:SCHED_MAX] if _hhmm(s.get("start")) and _hhmm(s.get("end"))]
        if a["mode"] == "event" and a.get("event"):
            ev = a["event"]
            doc["event"] = {k: ev[k] for k in ("src", "idx", "cmp", "value", "running", "on") if k in ev}
        out.append(doc)
    return out


def tz_minutes():
    """UTC offset of the display time zone now, in minutes (the meter evaluates schedules in local time)."""
    return int(datetime.now(timezone.utc).astimezone(settings.tz()).utcoffset().total_seconds() // 60)


def describe(a, device=None):
    """One line for the Info page: 'Schedule: ON Every day 18:00-23:59; ON Sat 01:00-02:00'."""
    a = dict(default(), **(a or {}))
    if a["mode"] == "schedule":
        parts = []
        for s in a.get("sched") or []:
            days = s.get("days") or []
            d = "Every day" if len(days) == 7 else ", ".join(DAYS[i] for i in days)
            parts.append(f"{'ON' if s.get('on', True) else 'OFF'} {d} {s.get('start')}-{s.get('end')}")
        return "Schedule: " + "; ".join(parts) + f" (otherwise {a.get('else', 'off').upper()})"
    if a["mode"] == "event" and a.get("event"):
        ev = a["event"]
        what = "ON" if ev.get("on", True) else "OFF"
        if ev.get("src") == "session":
            who = "the session" if not device or not device.is_mc or device.mc_mode != "independent" else \
                f"the session of {device.channel_name(ev.get('idx', 0))}"
            return f"Event: {what} while {who} is {'running' if ev.get('running', True) else 'ended'}"
        src = ev.get("src")
        if src in ("temp", "hum"):
            who = f"T{int(ev.get('idx', 0)) + 1}"
        elif src in ("amp", "watt"):
            who = "all channels" if ev.get("idx") == TOTAL else (device.channel_name(ev.get("idx", 0)) if device and device.is_mc else "the line")
        else:
            who = "the session" if not device or not device.is_mc or device.mc_mode != "independent" else device.channel_name(ev.get("idx", 0))
        cmp = "≥" if ev.get("cmp", "over") == "over" else "≤"
        return f"Event: {what} while {SOURCES.get(src, src)} {who} {cmp} {ev.get('value')} {UNITS.get(src, '')}".strip()
    return "Manual"
