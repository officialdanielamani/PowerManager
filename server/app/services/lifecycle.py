"""Disable / enable / unsync / remove a meter, and the "continue or new meter" choice on re-pair."""
from ..extensions import db
from ..models import (Channel, Command, Device, Event, Incident, Measurement, MeterViewer, Notification,
                      NotificationRule, PairingCode, Rollup, RollupDirty, Session, Temperature, now)
from . import commands, devconfig, hub, incidents, ingest, notify

HISTORY_MODELS = (Measurement, Temperature, Rollup, RollupDirty, Session, Incident, Command, Notification,
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
    notify.notify(device, "meter_status", f"Meter disabled by {user.username}: relay off, session ended")
    db.session.commit()
    commands.issue(device, "disable", {}, user)  # sent now, or when the meter reconnects


def enable(device, user):
    device.disabled_at = None
    (Command.query.filter_by(device_fk=device.id, c="disable", sent_at=None).update({"cancelled": True}))
    ingest.server_event(device, "enabled", {"by": user.username})
    notify.notify(device, "meter_status", f"Meter enabled by {user.username} (no session yet: Renew to start one)")
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
    subs = notify.subscribers(device, "meter_status")  # before: viewers may lose access
    _detach(device, "unsynced", user)
    ingest.server_event(device, "unsynced", {"by": user.username})
    notify.notify(device, "meter_status", f"Meter unsynced by {user.username}: it resets itself, history is kept",
                  subs=subs)
    db.session.commit()


def remove(device, user):
    """Meter factory-resets and every record of it is deleted. A small tombstone row stays so a
    meter that was offline still gets 'removed' (and resets) when it connects again."""
    subs, name = notify.subscribers(device, "meter_status"), device.name  # before the data is gone
    _detach(device, "removed", user)
    for model in HISTORY_MODELS:
        model.query.filter_by(device_fk=device.id).delete(synchronize_session=False)
    notify.notify(device, "meter_status", f"Meter removed by {user.username}: it resets itself, all its data was deleted",
                  subs=subs, name=name)
    PairingCode.query.filter_by(device_fk=device.id).update({"device_fk": None})
    device.purged = True
    device.name = device.device_id
    device.info = ""
    device.last_state, device.last_reading, device.last_info = {}, {}, {}
    db.session.commit()


# History that belongs to the hardware (moved to the archive). The meter's name, linked users and
# notification options stay with the live meter: it is still the same place / customer.
HW_HISTORY_MODELS = (Measurement, Temperature, Rollup, RollupDirty, Session, Incident, Command, Notification, Event)


def hardware_changed(device, new_caps, new_sig):
    """Different hardware / firmware type on the same ESP32 (same Device ID), e.g. PZEM AC -> rbAmp 2ch.
    The old history is kept for reference as an archived meter '<ID>~n'; the live meter starts clean
    with the right config for the new type. Returns the archive (None if there was no history)."""
    old_sig = device.hw_sig
    t = now()
    has_history = any(m.query.filter_by(device_fk=device.id).first() is not None
                      for m in (Measurement, Session, Incident))  # (server events alone are not history)
    archived = None
    if has_history:
        close_ongoing_incidents(device, "hardware_changed")
        _close_open_session(device, "hardware")
        n = 1
        while Device.query.filter_by(device_id=f"{device.device_id}~{n}").first() is not None:
            n += 1
        old_label = devconfig.hw_label(old_sig)
        archived = Device(device_id=f"{device.device_id}~{n}", name=f"{device.name} [{old_label}]"[:40],
                          info=device.info, caps=device.caps, cfg=device.cfg, fw=device.fw, hw=device.hw,
                          hw_sig=old_sig, pair_gen=device.pair_gen, paired_at=device.paired_at,
                          last_seen_at=device.last_seen_at, removed_at=t, last_reading=device.last_reading,
                          tare_a=device.tare_a, tare_w=device.tare_w, owner_fk=device.owner_fk,
                          currency=device.currency, price_per_kwh=device.price_per_kwh,
                          last_state={"archive": {"reason": "hardware", "at": t, "from": old_label,
                                                  "to": devconfig.hw_label(new_sig), "live": device.id}})
        db.session.add(archived)
        db.session.flush()
        for c in device.channels:
            archived.channels.append(Channel(idx=c.idx, label=c.label))
        for model in HW_HISTORY_MODELS:
            model.query.filter_by(device_fk=device.id).update({"device_fk": archived.id}, synchronize_session=False)
        for v in MeterViewer.query.filter_by(device_fk=device.id).all():  # viewers keep seeing the old history
            db.session.add(MeterViewer(device_fk=archived.id, user_fk=v.user_fk))
    # Live meter: new type, fresh config and history. A new pairing generation keeps the new session
    # numbers (which restart on the meter) apart from anything older.
    device.caps = dict(new_caps)
    device.hw_sig = new_sig
    device.pair_gen = (device.pair_gen or 1) + 1
    n = devconfig.channel_count(device)
    for c in list(device.channels):  # keep / reset the rows that still exist, drop extra ones
        if c.idx >= n:
            device.channels.remove(c)
            db.session.delete(c)
        else:
            c.label = f"CH{c.idx + 1}"
    have = {c.idx for c in device.channels}
    for i in range(n):
        if i not in have:
            device.channels.append(Channel(idx=i, label=f"CH{i + 1}"))
    device.cfg = devconfig.default_for(device)
    device.cfg_ver_wanted = (device.cfg_ver_wanted or 0) + 1
    device.cfg_ver_applied = 0
    device.cfg_error = None
    device.tare_a = device.tare_w = 0.0
    device.last_state, device.last_reading, device.last_info = {}, {}, {}
    ingest.server_event(device, "hardware_changed", {"from": old_sig, "to": new_sig,
                                                     "archive": archived.device_id if archived else None})
    msg = f"Hardware changed: {devconfig.hw_label(old_sig)} → {devconfig.hw_label(new_sig)}. Config reset to defaults"
    msg += f"; the old history is kept for reference as {archived.device_id}." if archived else "."
    notify.notify(device, "meter_status", msg)
    return archived


def resync_as_new(device):
    """The admin chose "new meter" for re-paired hardware: move the old history to an archived
    (unsynced) entry, and give the live meter a clean start."""
    n = 1
    while Device.query.filter_by(device_id=f"{device.device_id}~{n}").first() is not None:
        n += 1
    archived = Device(device_id=f"{device.device_id}~{n}", name=device.name, info=device.info,
                      caps=device.caps, cfg=device.cfg, fw=device.fw, hw=device.hw, pair_gen=device.pair_gen - 1,
                      paired_at=device.paired_at, last_seen_at=device.last_seen_at, removed_at=now(),
                      last_state={}, last_reading=device.last_reading, owner_fk=device.owner_fk,
                      currency=device.currency, price_per_kwh=device.price_per_kwh)
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
