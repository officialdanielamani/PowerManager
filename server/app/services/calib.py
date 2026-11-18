"""Zero & Tare wizard (docs/protocol.md §13): sensor checks on the meter, their evaluation, and the stored zero / tare per channel.

A sensor check is a command to the meter (`sensor_check`); it measures the raw values of one channel for at least 10 readings
(10 s) and reports statistics in an event. This module decides whether a check is good enough (the thresholds live here, so they
can change without a firmware update) and turns it into a zero or a tare offset, which goes to the meter in the config.
"""
from ..extensions import db
from ..models import now
from . import commands, devconfig, hub

MIN_READINGS = 10
MIN_SECS = 10
CHECK_MAX_AGE_S = 600          # an older check is not used any more
STABLE_A = (0.03, 0.03)        # readings are stable when max - min <= max(abs, rel * mean)
STABLE_W = (3.0, 0.05)
ZERO_MAX_A, ZERO_MAX_W = 1.0, 100.0   # a zero is small: more means a load is still connected
TARE_MIN_A = 0.01              # a tare needs something to measure
KINDS = ("zero", "tare")


def channel_count(device):
    return devconfig.channel_count(device) if device.is_mc else 1


def _bump(device):
    device.cfg_ver_wanted = (device.cfg_ver_wanted or 0) + 1


def push(device):
    """Send the config (with the new zero / tare) now if the meter is connected, else it gets it at the next connect."""
    hub.send(device.id, {"t": "cfg", **devconfig.to_device(device.cfg, device.cfg_ver_wanted, device.calib)})


def get(device, ch):
    return (device.calib or {}).get(str(ch)) or {}


def values(device, ch, kind):
    """(a, w) of a stored zero / tare, or (0, 0)."""
    v = get(device, ch).get(kind) or {}
    return float(v.get("a") or 0), float(v.get("w") or 0)


def prune(device):
    """Channels that no longer exist lose their zero / tare / check."""
    n = channel_count(device)
    for col in ("calib", "last_check"):
        cur = getattr(device, col) or {}
        keep = {k: v for k, v in cur.items() if str(k).isdigit() and int(k) < n}
        if keep != cur:
            setattr(device, col, keep)


# ---------------------------------------------------------------- sensor check

def start_check(device, ch, kind, user=None):
    """Ask the meter for a sensor check of channel `ch`. Returns (command, error)."""
    if kind not in KINDS or not isinstance(ch, int) or not 0 <= ch < channel_count(device):
        return None, "bad_channel"
    cmd, err = commands.issue(device, "sensor_check", {"ch": ch, "n": MIN_READINGS}, user)
    if err:
        return None, err
    cur = dict(device.last_check or {})
    cur[str(ch)] = {"state": "running", "kind": kind, "ref": cmd.id, "at": now(), "n": MIN_READINGS}
    device.last_check = cur
    db.session.commit()
    return cmd, None


def _set_check(device, ch, entry):
    cur = dict(device.last_check or {})
    cur[str(ch)] = entry
    device.last_check = cur


def on_ack(device, cmd, ok, err):
    """cmd_ack of sensor_check: a refusal ends the check at once."""
    ch = (cmd.params or {}).get("ch", 0)
    old = (device.last_check or {}).get(str(ch)) or {}
    if not ok and old.get("ref") == cmd.id:
        _set_check(device, ch, dict(old, state="failed", why=str(err or "refused"), at=now()))


def on_event(device, ev):
    """Event sensor_check from the meter: the statistics of the readings."""
    d = ev.data or {}
    ch = ev.ch if isinstance(ev.ch, int) else 0
    old = (device.last_check or {}).get(str(ch)) or {}
    stats = {k: d.get(k) for k in ("n", "secs", "a", "w", "v")}
    _set_check(device, ch, {"state": "done" if d.get("ok") else "failed", "kind": old.get("kind", "zero"), "ref": old.get("ref"),
                            "at": ev.ts, "stats": stats, "why": d.get("why")})


def _spread_ok(triple, limits):
    try:
        mean, mn, mx = (float(x) for x in triple)
    except (TypeError, ValueError):
        return False, 0.0
    return (mx - mn) <= max(limits[0], limits[1] * abs(mean)), mx - mn


def evaluate(device, ch, kind):
    """Is the last check of channel `ch` good for a zero / a tare? -> {ok, reasons, a, w, stats, state}

    a / w = the offset it would set (zero: the raw mean; tare: the raw mean minus the zero)."""
    chk = (device.last_check or {}).get(str(ch)) or {}
    out = {"state": chk.get("state", "none"), "kind": chk.get("kind"), "ok": False, "reasons": [], "a": None, "w": None,
           "stats": chk.get("stats"), "at": chk.get("at")}
    if chk.get("state") != "done" or not chk.get("stats"):
        if chk.get("state") == "failed":
            out["reasons"].append({"sensor": "The sensor did not give enough valid readings.", "cancelled": "The check was cancelled.",
                                   "busy": "The meter is busy with another check.", "no_sensor": "The sensor is not answering.",
                                   "offline": "The meter is offline."}.get(chk.get("why"), f"The check failed ({chk.get('why') or 'no answer'})."))
        return out
    if chk.get("kind") != kind:
        out["reasons"].append(f"The last check was started for a {chk.get('kind')}: run a new one.")
        return out
    s = chk["stats"]
    if now() - int(chk.get("at") or 0) > CHECK_MAX_AGE_S:
        out["reasons"].append("The check is older than 10 minutes: run a new one.")
    if int(s.get("n") or 0) < MIN_READINGS:
        out["reasons"].append(f"Only {s.get('n') or 0} readings (at least {MIN_READINGS} are needed).")
    if float(s.get("secs") or 0) < MIN_SECS:
        out["reasons"].append(f"The check took {s.get('secs') or 0} s (at least {MIN_SECS} s are needed).")
    ok_a, spread_a = _spread_ok(s.get("a"), STABLE_A)
    ok_w, spread_w = _spread_ok(s.get("w"), STABLE_W)
    if not ok_a:
        out["reasons"].append(f"The current is not stable (it varied by {spread_a:.3f} A).")
    if not ok_w:
        out["reasons"].append(f"The power is not stable (it varied by {spread_w:.1f} W).")
    try:
        mean_a, mean_w = float(s["a"][0]), float(s["w"][0])
    except (KeyError, TypeError, ValueError, IndexError):
        out["reasons"].append("The readings are incomplete.")
        return out
    if kind == "zero":
        if mean_a > ZERO_MAX_A or mean_w > ZERO_MAX_W:
            out["reasons"].append(f"A load seems to be connected ({mean_a:.2f} A, {mean_w:.0f} W): zeroing needs no load "
                                  f"(at most {ZERO_MAX_A:g} A and {ZERO_MAX_W:g} W).")
        out["a"], out["w"] = round(max(0.0, mean_a), 4), round(max(0.0, mean_w), 2)
    else:
        za, zw = values(device, ch, "zero")
        a, w = max(0.0, mean_a - za), max(0.0, mean_w - zw)
        if a < TARE_MIN_A:
            out["reasons"].append("No load was measured: connect the load that should count as 0, then check again.")
        out["a"], out["w"] = round(a, 4), round(w, 2)
    out["ok"] = not out["reasons"]
    return out


# ---------------------------------------------------------------- set / clear

def apply(device, ch, kind, user=None):
    """Store the zero / tare from the last good check. Returns (values, errors)."""
    if kind not in KINDS or not isinstance(ch, int) or not 0 <= ch < channel_count(device):
        return None, ["No such channel"]
    ev = evaluate(device, ch, kind)
    if not ev["ok"]:
        return None, ev["reasons"] or ["No sensor check yet."]
    cur = dict(device.calib or {})
    entry = dict(cur.get(str(ch)) or {})
    entry[kind] = {"a": ev["a"], "w": ev["w"], "at": now()}
    entry["by"] = getattr(user, "username", None) or entry.get("by")
    cur[str(ch)] = entry
    device.calib = cur
    if kind == "zero":   # a tare measured before is relative to the old zero: ask for a new one
        pass
    from . import ingest  # (ingest imports this module)
    _bump(device)
    ingest.server_event(device, f"{kind}_set", {"ch": ch, "a": ev["a"], "w": ev["w"], "by": entry["by"]})
    db.session.commit()
    push(device)
    return {"a": ev["a"], "w": ev["w"]}, []


def clear(device, ch, what, user=None):
    """Remove the zero, the tare, or both of a channel."""
    if what not in KINDS + ("all",) or not isinstance(ch, int) or not 0 <= ch < channel_count(device):
        return False
    cur = dict(device.calib or {})
    entry = dict(cur.get(str(ch)) or {})
    for k in (KINDS if what == "all" else (what,)):
        entry.pop(k, None)
    if any(k in entry for k in KINDS):
        cur[str(ch)] = entry
    else:
        cur.pop(str(ch), None)
    device.calib = cur
    from . import ingest
    _bump(device)
    ingest.server_event(device, "calibration_cleared", {"ch": ch, "what": what, "by": getattr(user, "username", None)})
    db.session.commit()
    push(device)
    return True


def view(device):
    """Per-channel state for the Zero & Tare page."""
    out = []
    for i in range(channel_count(device)):
        c = get(device, i)
        chk = (device.last_check or {}).get(str(i)) or {}
        out.append({"ch": i, "name": device.channel_name(i) if device.is_mc else "Measured line",
                    "zero": c.get("zero"), "tare": c.get("tare"), "by": c.get("by"),
                    "check": chk or None, "eval": evaluate(device, i, chk.get("kind") or "zero") if chk else None})
    return out
