"""AUX relay configuration: name + automation (manual / schedule / event) per AUX relay.

Stored in device.cfg["aux"] in display form (local "HH:MM", day list) and sent to the meter as minutes of
the local day + a day bit mask, with "tz_min" (UTC offset of the display time zone) so the meter evaluates
schedules in local time on its own, also offline. See docs/protocol.md (config "aux").
"""
from datetime import date, datetime, timezone

from . import settings

MODES = {"manual": "Manual (ON / OFF buttons)", "schedule": "Schedule", "event": "Event"}
SOURCES = {"temp": "Temperature", "hum": "Humidity", "amp": "Current (A)", "watt": "Power (W)",
           "kwh": "kWh balance", "session": "Session", "tamper": "Tamper", "trip": "Over-current trip",
           "volt": "Voltage protection", "wifi": "No Wi-Fi", "server": "No server / internet",
           "ups": "UPS power (mains failed)", "bat": "Battery %", "reboot": "Wait for reboot (hardware change)"}
UNITS = {"temp": "°C", "hum": "%RH", "amp": "A", "watt": "W", "kwh": "kWh", "bat": "%"}
FLAGS = ("tamper", "trip", "volt", "wifi", "server", "ups", "reboot")  # on / off conditions ("active" or "not active")
DAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
SCHED_MAX = 14
TOTAL = 255  # amp / watt of all channels together
ANY = 254    # any channel


def temp_count(device):
    if device is None:
        return 1
    return max(int((device.caps or {}).get("temp") or 0), len((device.last_reading or {}).get("t") or []))


def sources(device):
    """Event sources this meter can use: only what its firmware was built with (hello.caps) - no
    temperature / humidity without the sensor, no tamper / UPS / battery without that part."""
    if device is None:
        return dict(SOURCES)
    caps = device.caps or {}
    need = {"temp": temp_count(device), "hum": temp_count(device) and caps.get("hum"), "tamper": caps.get("tamper"),
            "volt": caps.get("voltage") is not False, "ups": caps.get("ups"), "bat": caps.get("battery"), "reboot": caps.get("hw_cfg")}
    return {k: label for k, label in SOURCES.items() if need.get(k, True)}


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


def _date(raw):
    try:
        d = date.fromisoformat(str(raw or "").strip())
        return d if 2020 <= d.year <= 2199 else None
    except ValueError:
        return None


def parse_form(form, n, errors, device=None):
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
            if not start or not end:
                errors.append(f"{label} schedule {k + 1}: enter a start and an end time")
                continue
            row = {"on": form.get(f"aux_s{k}_on_{j}", "on") == "on", "start": start, "end": end}
            if form.get(f"aux_s{k}_kind_{j}") == "date":
                d = _date(form.get(f"aux_s{k}_date_{j}"))
                if not d:
                    errors.append(f"{label} schedule {k + 1}: pick a date")
                    continue
                row["date"] = d.isoformat()
                row["yearly"] = form.get(f"aux_s{k}_yearly_{j}") == "on"
            else:
                days = sorted({int(d) for d in form.getlist(f"aux_s{k}_days_{j}") if str(d).isdigit() and int(d) < 7})
                if not days:
                    errors.append(f"{label} schedule {k + 1}: pick at least one day")
                    continue
                row["days"] = days
            a["sched"].append(row)
        allowed = sources(device)
        src = form.get(f"aux_ev_src_{j}", "")
        if src not in allowed and a["mode"] == "event":
            errors.append(f"{label} event: choose what the relay reacts to")
        if src in allowed:
            idx = form.get(f"aux_ev_idx_{j}", "0")
            idx = int(idx) if str(idx).isdigit() and (int(idx) < 16 or int(idx) == TOTAL or int(idx) == ANY) else 0
            if src in ("tamper", "volt", "wifi", "server", "ups", "bat"):
                idx = 0
            ev = {"src": src, "idx": idx, "on": form.get(f"aux_ev_on_{j}", "on") == "on"}
            if src == "session":
                ev["running"] = form.get(f"aux_ev_running_{j}", "running") == "running"
            elif src in FLAGS:
                ev["active"] = form.get(f"aux_ev_active_{j}", "active") == "active"
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
            doc["sched"] = []
            for s in (a.get("sched") or [])[:SCHED_MAX]:
                if not (_hhmm(s.get("start")) and _hhmm(s.get("end"))):
                    continue
                row = {"on": bool(s.get("on", True)), "start": _minutes(s["start"]), "end": _minutes(s["end"])}
                d = _date(s.get("date"))
                if d:
                    row["date"] = [0 if s.get("yearly") else d.year, d.month, d.day]
                else:
                    row["days"] = sum(1 << int(x) for x in s.get("days") or [])
                doc["sched"].append(row)
        if a["mode"] == "event" and a.get("event"):
            ev = a["event"]
            doc["event"] = {k: ev[k] for k in ("src", "idx", "cmp", "value", "running", "active", "on") if k in ev}
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
            if s.get("date"):
                d = (s["date"][5:] + " every year") if s.get("yearly") else s["date"]
            else:
                d = "Every day" if len(days) == 7 else ", ".join(DAYS[i] for i in days)
            parts.append(f"{'ON' if s.get('on', True) else 'OFF'} {d} {s.get('start')}-{s.get('end')}")
        return "Schedule: " + "; ".join(parts) + f" (otherwise {a.get('else', 'off').upper()})"
    if a["mode"] == "event" and a.get("event"):
        ev = a["event"]
        what = "ON" if ev.get("on", True) else "OFF"
        mc = bool(device and device.is_mc)
        if ev.get("src") in FLAGS:
            who = ""
            if ev.get("src") == "trip" and mc:
                who = " (any channel)" if ev.get("idx") == ANY else f" ({device.channel_name(ev.get('idx', 0))})"
            state = "" if ev.get("active", True) else " is clear"
            return f"Event: {what} while {SOURCES[ev['src']]}{who}{state}"
        if ev.get("src") == "session":
            who = "the session" if not device or not device.is_mc or device.mc_mode != "independent" else \
                ("any session" if ev.get("idx") == ANY else f"the session of {device.channel_name(ev.get('idx', 0))}")
            return f"Event: {what} while {who} is {'running' if ev.get('running', True) else 'ended'}"
        src = ev.get("src")
        if src in ("temp", "hum"):
            who = f"T{int(ev.get('idx', 0)) + 1}"
        elif src == "bat":
            who = ""
        elif src in ("amp", "watt"):
            who = "all channels" if ev.get("idx") == TOTAL else "any channel" if ev.get("idx") == ANY else (device.channel_name(ev.get("idx", 0)) if device and device.is_mc else "the line")
        else:
            who = "the session" if not device or not device.is_mc or device.mc_mode != "independent" else \
                ("any session" if ev.get("idx") == ANY else device.channel_name(ev.get("idx", 0)))
        cmp = "≥" if ev.get("cmp", "over") == "over" else "≤"
        return " ".join(f"Event: {what} while {SOURCES.get(src, src)} {who} {cmp} {ev.get('value')} {UNITS.get(src, '')}".split())
    return "Manual"
