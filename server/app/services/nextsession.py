"""Schedule Next Session (per channel, independent mode): new limits and session details that replace the current ones at a set
time. The server executes it (scheduler, every 30 s): the running session of the channel ends, the new values are sent to the
meter, and with Auto renew a new session starts on them. Afterwards the schedule is cleared. docs/server-ui.md."""
import copy

from ..extensions import db
from ..models import Device, now
from . import commands, devconfig, hub, notify, settings

MIN_AHEAD_S = 300            # at least 5 minutes in the future when it is saved
MAX_AHEAD_S = 366 * 86400
FIELDS = ("amp_max", "kwh_max", "kwh_action", "sess_mode", "dur_min", "daily_at", "auto")


def _values(device, ch, body, errors):
    """The new limits / session details of the channel from the request, validated like the Configuration form."""
    label = f"CH{ch + 1}: "
    rating = devconfig.amp_rating(device)
    c = {}
    c["amp_max"] = devconfig._num(body, "amp_max", float, 0, rating, f"{label}max A", errors, 0.0)
    c["kwh_max"] = devconfig._num(body, "kwh_max", float, 0, 100000, f"{label}max kWh", errors, 0.0)
    a = body.get("kwh_action", devconfig.DEFAULT_KWH_ACTION)
    c["kwh_action"] = a if a in devconfig.KWH_ACTIONS else devconfig.DEFAULT_KWH_ACTION
    c.update(devconfig._parse_sess(body, "", errors, label))
    return c


def _at(body, errors):
    """When it happens: relative (days / hours / minutes from now) or a calendar date + time in the display time zone."""
    t = now()
    if body.get("when") == "cal":
        at = settings.local_to_epoch(str(body.get("date") or ""))
        if at is None:
            errors.append("Pick the date and time.")
            return None
    else:
        try:
            d, h, m = (int(body.get(k) or 0) for k in ("d", "h", "m"))
        except (TypeError, ValueError):
            errors.append("Next: days, hours and minutes must be numbers.")
            return None
        if min(d, h, m) < 0:
            errors.append("Next: days, hours and minutes cannot be negative.")
            return None
        at = t + d * 86400 + h * 3600 + m * 60
    if at < t + MIN_AHEAD_S:
        errors.append("The time must be at least 5 minutes in the future.")
        return None
    if at > t + MAX_AHEAD_S:
        errors.append("The time is too far ahead (a year at most).")
        return None
    return at


def schedule(device, ch, body, user):
    """Store the schedule of channel `ch`. Returns (next dict, errors)."""
    errors = []
    if not device.is_mc or device.mc_mode != "independent":
        return None, ["Schedule Next Session is for the channels of a meter in Independent mode."]
    if not isinstance(ch, int) or not 0 <= ch < devconfig.channel_count(device):
        return None, ["No such channel."]
    vals = _values(device, ch, body, errors)
    at = _at(body, errors)
    if errors:
        return None, errors
    cfg = copy.deepcopy(device.cfg or {})
    cfg["ch"][ch]["next"] = dict(vals, at=at, by=user.username if user else None, set_at=now())
    device.cfg = cfg
    db.session.commit()
    return view_one(cfg["ch"][ch]), []


def clear(device, ch):
    cfg = copy.deepcopy(device.cfg or {})
    try:
        had = cfg["ch"][ch].pop("next", None)
    except (IndexError, KeyError, TypeError):
        return False
    device.cfg = cfg
    db.session.commit()
    return bool(had)


def view_one(c):
    n = (c or {}).get("next")
    if not n:
        return None
    return dict(n, at_local=settings.fmt_ts(n["at"], with_seconds=False))


def execute(device, ch):
    """The time has come for channel `ch`: replace its settings, end its session, start a new one when Auto renew is ticked."""
    cfg = copy.deepcopy(device.cfg or {})
    n = cfg["ch"][ch].pop("next", None)
    if not n:
        return
    for k in FIELDS:
        if k in n:
            cfg["ch"][ch][k] = n[k]
    device.cfg = cfg
    device.cfg_ver_wanted = (device.cfg_ver_wanted or 0) + 1
    device.cfg_error = None
    db.session.commit()
    if hub.is_online(device.id):
        hub.send(device.id, {"t": "cfg", **devconfig.to_device(device.cfg, device.cfg_ver_wanted, device.calib)})
    disabled = bool(cfg["ch"][ch].get("disabled"))
    if not disabled and not device.disabled_at:
        # The running session ends at this moment; with Auto renew the next one starts on the new settings (renew ends the old one).
        commands.issue(device, "renew" if n.get("auto") else "end", {"ch": ch})
    from . import ingest
    ingest.server_event(device, "next_session", {"ch": ch, "at": n.get("at"), "by": n.get("by"), "amp_max": n.get("amp_max"),
                                                  "kwh_max": n.get("kwh_max"), "auto": bool(n.get("auto"))})
    notify.notify(device, "channel_status",
                  f"{device.channel_name(ch)}: the scheduled session settings are now in use (max {n.get('amp_max')} A, "
                  f"{n.get('kwh_max') or 'no'} kWh limit)" + (", a new session started" if n.get("auto") and not disabled else ""), ch=ch)
    db.session.commit()


def run():
    """Scheduler job: execute every schedule that is due."""
    t = now()
    for device in Device.query.filter(Device.removed_at.is_(None)).all():
        if not device.is_mc or device.mc_mode != "independent":
            continue
        for i, c in enumerate((device.cfg or {}).get("ch") or []):
            n = c.get("next")
            if n and int(n.get("at") or 0) <= t:
                execute(device, i)
