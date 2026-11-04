"""Historical events: pair a start event with its end event (docs/data-model.md `incident`)."""
from ..extensions import db
from ..models import Incident

TYPES = {
    "power": "Power disconnected",
    "offline": "Offline",
    "trip": "Power trip",
    "tamper": "Tamper",
    "voltage": "Voltage out of range",
    "sensor": "Sensor error",
}


def find_open(device_pk, itype, ch=None):
    return (Incident.query
            .filter_by(device_fk=device_pk, type=itype, ch=ch, ended_at=None)
            .order_by(Incident.id.desc()).first())


def open_incident(device_pk, itype, ts, ch=None, event=None, detail=None, estimated=False):
    existing = find_open(device_pk, itype, ch)
    if existing is not None:
        # Opened earlier by a state sync (on reconnect) before the buffered start event
        # arrived: use the real start time from the event.
        if (existing.detail or {}).get("opened_by") == "state_sync" and int(ts) < existing.started_at:
            existing.started_at = int(ts)
            existing.start_event_fk = event.id if event else existing.start_event_fk
            existing.detail = {k: v for k, v in (existing.detail or {}).items() if k != "opened_by"}
            if detail:
                existing.detail = {**existing.detail, **detail}
            return existing
        return None
    inc = Incident(device_fk=device_pk, type=itype, ch=ch, started_at=int(ts),
                   start_event_fk=event.id if event else None,
                   detail=dict(detail or {}), time_estimated=estimated)
    db.session.add(inc)
    return inc


def close_incident(device_pk, itype, ts, ch=None, event=None, detail=None, estimated=False):
    inc = find_open(device_pk, itype, ch)
    if inc is None:
        # Closed earlier by a state sync before the buffered end event arrived: fix the end time.
        last = (Incident.query.filter_by(device_fk=device_pk, type=itype, ch=ch)
                .filter(Incident.ended_at.isnot(None)).order_by(Incident.id.desc()).first())
        if (last is not None and (last.detail or {}).get("closed_by") == "state_sync"
                and last.started_at <= int(ts) <= last.ended_at):
            last.ended_at = int(ts)
            last.duration_s = last.ended_at - last.started_at
            last.end_event_fk = event.id if event else last.end_event_fk
            last.detail = {k: v for k, v in (last.detail or {}).items() if k != "closed_by"}
            return last
        return None
    ts = max(int(ts), inc.started_at)
    inc.ended_at = ts
    inc.duration_s = ts - inc.started_at
    inc.end_event_fk = event.id if event else None
    if detail:
        merged = dict(inc.detail or {})
        merged.update(detail)
        inc.detail = merged
    if estimated:
        inc.time_estimated = True
    return inc


def fmt_duration(seconds):
    if seconds is None:
        return "–"
    seconds = int(seconds)
    d, rest = divmod(seconds, 86400)
    h, rest = divmod(rest, 3600)
    m, s = divmod(rest, 60)
    if d:
        return f"{d}d {h}h {m}m"
    if h:
        return f"{h}h {m:02d}m {s:02d}s"
    if m:
        return f"{m}m {s:02d}s"
    return f"{s}s"
