"""Disable / enable / unsync / remove a meter, and the "continue or new meter" choice on re-pair."""
from ..extensions import db
from ..models import (Channel, Command, Device, Event, Incident, Measurement, MeterViewer, Notification,
                      NotificationRule, PairingCode, Rollup, RollupDirty, Session, now)
from . import commands, hub, incidents, ingest

HISTORY_MODELS = (Measurement, Rollup, RollupDirty, Session, Incident, Command, Notification,
                  NotificationRule, MeterViewer, Event)


def _close_open_session(device, reason):
    """End the current session record now (the meter does the same when it gets the command)."""
    sid = ((device.last_state or {}).get("sess") or {}).get("sid")
    if isinstance(sid, int):
        s = Session.query.filter_by(device_fk=device.id, gen=device.pair_gen, sid=sid).first()
        if s is not None and s.ended_at is None:
            s.ended_at, s.end_reason = now(), reason


def close_ongoing_incidents(device, why):
    t = now()
    for inc in Incident.query.filter_by(device_fk=device.id, ended_at=None).all():
        incidents.close_incident(device.id, inc.type, t, ch=inc.ch, detail={"closed_by": why})


def disable(device, user):
    """Paired but read-only: session ends, relay off (no manual ON), no new data is recorded."""
    device.disabled_at = now()
    _close_open_session(device, "disabled")
    close_ongoing_incidents(device, "disabled")
    (Command.query.filter_by(device_fk=device.id, sent_at=None, cancelled=False).update({"cancelled": True}))
    ingest.server_event(device, "disabled", {"by": user.username})
    db.session.commit()
    commands.issue(device, "disable", {}, user)  # sent now, or when the meter reconnects


def enable(device, user):
    device.disabled_at = None
    (Command.query.filter_by(device_fk=device.id, c="disable", sent_at=None).update({"cancelled": True}))
    ingest.server_event(device, "enabled", {"by": user.username})
    db.session.commit()
    commands.issue(device, "enable", {}, user)  # no new session: the admin renews when ready


def _detach(device, why, user):
    hub.send(device.id, {"t": "cmd", "c": "remove", "ref": 0})  # meter factory-resets (or on next connect: 4403)
    _close_open_session(device, why)
    close_ongoing_incidents(device, why)
    device.removed_at = now()
    device.disabled_at = None
    device.secret_hash = None
    device.online = False
    device.resync_pending = False


def unsync(device, user):
    """Meter factory-resets; all history stays (listed under unsynced meters)."""
    _detach(device, "unsynced", user)
    ingest.server_event(device, "unsynced", {"by": user.username})
    db.session.commit()


def remove(device, user):
    """Meter factory-resets and every record of it is deleted. A small tombstone row stays so a
    meter that was offline still gets 'removed' (and resets) when it connects again."""
    _detach(device, "removed", user)
    for model in HISTORY_MODELS:
        model.query.filter_by(device_fk=device.id).delete(synchronize_session=False)
    PairingCode.query.filter_by(device_fk=device.id).update({"device_fk": None})
    device.purged = True
    device.name = device.device_id
    device.info = ""
    device.last_state, device.last_reading, device.last_info = {}, {}, {}
    db.session.commit()


def resync_as_new(device):
    """The admin chose "new meter" for re-paired hardware: move the old history to an archived
    (unsynced) entry, and give the live meter a clean start."""
    n = 1
    while Device.query.filter_by(device_id=f"{device.device_id}~{n}").first() is not None:
        n += 1
    archived = Device(device_id=f"{device.device_id}~{n}", name=device.name, info=device.info,
                      caps=device.caps, cfg=device.cfg, fw=device.fw, hw=device.hw, pair_gen=device.pair_gen - 1,
                      paired_at=device.paired_at, last_seen_at=device.last_seen_at, removed_at=now(),
                      last_state={}, last_reading=device.last_reading)
    db.session.add(archived)
    db.session.flush()
    for c in device.channels:
        archived.channels.append(Channel(idx=c.idx, label=c.label))
    for model in HISTORY_MODELS:
        model.query.filter_by(device_fk=device.id).update({"device_fk": archived.id}, synchronize_session=False)
    device.name, device.info = device.device_id, ""
    device.resync_pending = False
    device.last_reading = {}
    db.session.commit()
    return archived
