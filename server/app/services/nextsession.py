"""Schedule Next Session: new limits and session details that replace the current ones at a set time.

Where it is stored: `cfg["ch"][i]["next"]` (a channel of a meter in Independent mode) or `cfg["next"]` (a single-channel meter, or the
whole meter in Shared mode). The settings go to the meter inside its configuration (docs/protocol.md §3, `next`) and the METER carries
them out at the set time on its own clock, so it also works while the meter is offline (the events it records meanwhile are sent when it
is back). The server then copies the values into its own configuration (`device_applied`). docs/server-ui.md."""
import copy

from ..extensions import db
from ..models import now
from . import devconfig, hub, notify, settings

MIN_AHEAD_S = 300            # at least 5 minutes in the future when it is saved
MAX_AHEAD_S = 366 * 86400
FIELDS = ("amp_max", "kwh_max", "kwh_action", "sess_mode", "dur_min", "daily_at", "week", "auto")


def scope_ok(device, ch):
    """(ok, error) for the scope: a channel number (Independent mode) or None (single channel / Shared mode: the whole meter)."""
    indep = device.is_mc and device.mc_mode == "independent"
    if indep:
        if not isinstance(ch, int) or not 0 <= ch < devconfig.channel_count(device):
            return False, "No such channel."
    elif ch is not None:
        return False, "In Shared mode the schedule is for the whole meter."
    return True, None


def has_amp(device, ch):
    """The max current is part of the schedule for a channel and for a single-channel meter, not for the whole of a Shared meter."""
    return ch is not None or not device.is_mc


def _values(device, ch, body, errors):
    """The new limits / session details from the request, validated like the Configuration form."""
    label = f"CH{ch + 1}: " if ch is not None else ""
    rating = devconfig.amp_rating(device)
    c = {}
    if has_amp(device, ch):
        c["amp_max"] = devconfig._num(body, "amp_max", float, 0, rating, f"{label}max A", errors, 0.0)
    c["kwh_max"] = devconfig._num(body, "kwh_max", float, 0, 100000, f"{label}max kWh", errors, 0.0)
    a = body.get("kwh_action", devconfig.DEFAULT_KWH_ACTION)
    c["kwh_action"] = a if a in devconfig.KWH_ACTIONS else devconfig.DEFAULT_KWH_ACTION
    week = body.get("week")
    form = dict(body)
    if isinstance(week, dict):   # {"mon": "08:00"}: the same field names as the Configuration form
        for d in devconfig.WEEKDAYS:
            if d in week:
                form[f"wk_{d}"], form[f"wk_{d}_at"] = "on", week[d]
    c.update(devconfig._parse_sess(form, "", errors, label))
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


def _node(cfg, ch):
    """The dict that holds the `next` of the scope."""
    return cfg["ch"][ch] if ch is not None else cfg


def _push(device):
    """The meter gets the new configuration (with or without `next`); offline: it gets it when it connects."""
    device.cfg_ver_wanted = (device.cfg_ver_wanted or 0) + 1
    device.cfg_error = None
    db.session.commit()
    if hub.is_online(device.id):
        hub.send(device.id, {"t": "cfg", **devconfig.to_device(device.cfg, device.cfg_ver_wanted, device.calib)})


def schedule(device, ch, body, user):
    """Store the schedule. Returns (next dict, errors)."""
    errors = []
    ok, err = scope_ok(device, ch)
    if not ok:
        return None, [err]
    vals = _values(device, ch, body, errors)
    at = _at(body, errors)
    if errors:
        return None, errors
    cfg = copy.deepcopy(device.cfg or {})
    node = _node(cfg, ch)
    node["next"] = dict(vals, at=at, by=user.username if user else None, set_at=now())
    device.cfg = cfg
    _push(device)
    return view_one(node), []


def clear(device, ch):
    ok, _ = scope_ok(device, ch)
    if not ok:
        return False
    cfg = copy.deepcopy(device.cfg or {})
    had = _node(cfg, ch).pop("next", None)
    if not had:
        return False
    device.cfg = cfg
    _push(device)
    return True


def view_one(c):
    n = (c or {}).get("next")
    if not n:
        return None
    return dict(n, at_local=settings.fmt_ts(n["at"], with_seconds=False))


def _replace(cfg, ch, n, device):
    """Copy the scheduled settings over the current ones (the three places the settings live)."""
    for k in FIELDS:
        if k not in n:
            continue
        if ch is not None or k not in ("amp_max", "kwh_max", "kwh_action") or device.is_mc:
            _node(cfg, ch)[k] = n[k]
        else:   # single-channel meter: the limits of its one channel
            cfg["ch"][0][k] = n[k]


def _announce(device, ch, n):
    who = device.channel_name(ch) + ": " if ch is not None else ""
    limit = f"max {n.get('amp_max')} A, " if n.get("amp_max") is not None else ""
    notify.notify(device, "channel_status",
                  f"{who}the scheduled session settings are now in use ({limit}{n.get('kwh_max') or 'no'} kWh limit)"
                  + (", a new session started" if n.get("auto") else ""), ch=ch)


def device_applied(device, ch, at):
    """The meter has carried out the schedule itself (event `next_session`, possibly long after it happened: it was offline).
    The server takes over the same values. Nothing to send: the meter already runs on them."""
    ok, _ = scope_ok(device, ch)
    cfg = copy.deepcopy(device.cfg or {})
    n = _node(cfg, ch).get("next") if ok else None
    if not n or int(n.get("at") or 0) != int(at or 0):
        # Cleared or changed on the server meanwhile: the meter must go back to what the server holds.
        _push(device)
        return
    _node(cfg, ch).pop("next", None)
    _replace(cfg, ch, n, device)
    device.cfg = cfg
    db.session.commit()
    _announce(device, ch, n)

