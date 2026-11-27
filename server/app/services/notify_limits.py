"""Threshold notifications that the server works out from the meter's live state (every 30 s): remaining kWh / time of a
session, low battery, temperature above a limit chosen by the user. Each one is sent once and armed again when the value
recovers (`NotificationRule.fired`)."""
from ..extensions import db
from ..models import Device, NotificationRule, now
from . import hub, notify

TYPES = ("kwh_remaining", "time_remaining", "battery_low", "temp_high")
STALE_S = 300     # a state older than this is not used (the meter is offline)


def _sessions(device):
    st = device.last_state or {}
    if device.is_mc and st.get("mode") == "independent":
        return [(s.get("ch"), s) for s in st.get("sessions") or [] if isinstance(s, dict)]
    s = st.get("sess")
    return [(None, s)] if isinstance(s, dict) else []


def _name(device, ch):
    return f"{device.channel_name(ch)}: " if device.is_mc and isinstance(ch, int) else ""


def _hhmm(minutes):
    return f"{minutes // 60}:{minutes % 60:02d}"


def _fire(device, rule, key, ntype, text, ch=None):
    """Send to the owner of the rule and remember it."""
    from ..models import User
    user = db.session.get(User, rule.user_fk)
    if user is not None:
        notify.notify(device, ntype, text, only_user=user, ch=ch)


def _evaluate(device, rule):
    t = now()
    st = device.last_state or {}
    if not st or t - int(st.get("at") or 0) > STALE_S or not hub.is_online(device.id):
        return
    fired = dict(rule.fired or {})
    before = dict(fired)
    x = int(rule.param or 0)
    if rule.type in ("kwh_remaining", "time_remaining") and x:
        for ch, s in _sessions(device):
            if rule.channels is not None and ch is not None and int(ch) not in [int(c) for c in rule.channels]:
                continue
            sid = s.get("sid")
            if not isinstance(sid, int) or sid <= 0 or s.get("status") != "active" or s.get("ended"):
                continue
            key = f"{rule.type}:{ch}"
            if rule.type == "kwh_remaining":
                try:
                    kwh, mx = float(s.get("kwh")), float(s.get("max"))
                except (TypeError, ValueError):
                    continue
                if mx <= 0:
                    continue
                left = (mx - kwh) / mx * 100.0
                if left <= x and fired.get(key) != sid:
                    fired[key] = sid
                    _fire(device, rule, key, "kwh_remaining",
                          f"{_name(device, ch)}{max(left, 0):.0f}% of the session kWh is left ({max(mx - kwh, 0):.3f} of {mx:g} kWh)", ch)
            else:
                nr = s.get("next_reset")
                if not isinstance(nr, int):
                    continue
                left_s = nr - t
                if 0 < left_s <= x * 60 and fired.get(key) != sid:
                    fired[key] = sid
                    _fire(device, rule, key, "time_remaining", f"{_name(device, ch)}{_hhmm(max(left_s // 60, 1))} (h:mm) of the session is left", ch)
    elif rule.type == "battery_low" and x:
        bat = st.get("bat")
        if isinstance(bat, (int, float)):
            if bat <= x and not fired.get("bat"):
                fired["bat"] = True
                _fire(device, rule, "bat", "battery_low", f"Battery is at {bat}% (below {x}%)")
            elif bat > x + 5:
                fired["bat"] = False
    elif rule.type == "temp_high":
        temps = [v for v in ((device.last_reading or {}).get("t") or []) if isinstance(v, (int, float))]
        if temps and rule.param is not None:
            hot = max(temps)
            if hot >= x and not fired.get("temp"):
                fired["temp"] = True
                _fire(device, rule, "temp", "temp_high", f"Temperature {hot} °C is above {x} °C")
            elif hot < x - 2:
                fired["temp"] = False
    if fired != before:
        rule.fired = fired


def run():
    for rule in NotificationRule.query.filter(NotificationRule.enabled.is_(True), NotificationRule.type.in_(TYPES)).all():
        device = db.session.get(Device, rule.device_fk)
        if device is None or device.removed_at or device.disabled_at:
            continue
        _evaluate(device, rule)
    db.session.commit()
