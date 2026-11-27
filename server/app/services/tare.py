"""Tare: a load that should count as 0 on one channel (the meter's own supply, a charger ...). docs/protocol.md §13.

Raw values are always stored as measured (and already zero-corrected by the meter); tared values are computed when displayed
or exported. The offsets are part of the meter config (per channel), so with "tare_limits" (default) the meter itself uses
tared values for its kWh limit and max current. Voltage is never tared.
"""


def offsets(device, ch=0):
    """(amps, watts) of channel `ch`, or None when it has no tare."""
    t = ((getattr(device, "calib", None) or {}).get(str(ch)) or {}).get("tare") or {}
    a, w = float(t.get("a") or 0), float(t.get("w") or 0)
    return (a, w) if (a or w) else None


def any_tare(device):
    """True when any channel of the meter has a tare (the Raw | Tared switch is offered)."""
    return any(offsets(device, int(k)) for k in (getattr(device, "calib", None) or {}) if str(k).isdigit())


def total(device, channels):
    """Sum of the tares of `channels` (whole-meter values of a multi-channel meter), or None."""
    parts = [offsets(device, c) for c in channels]
    parts = [p for p in parts if p]
    return (sum(p[0] for p in parts), sum(p[1] for p in parts)) if parts else None


def amps(value, tare):
    return None if value is None else (max(0.0, value - tare[0]) if tare else value)


def watts(value, tare):
    return None if value is None else (max(0.0, value - tare[1]) if tare else value)


def kwh(value, seconds, tare):
    """Energy minus the tare power over `seconds` (never below 0)."""
    if value is None or not tare:
        return value
    return max(0.0, value - tare[1] * max(0, seconds) / 3_600_000.0)


def reading(ch, tare):
    """A channel reading dict {v, a, w, pf, hz, e} with current/power tared (energy register untouched)."""
    if not tare or not isinstance(ch, dict):
        return ch
    out = dict(ch)
    out["a"] = round(amps(ch.get("a"), tare), 3) if ch.get("a") is not None else None
    out["w"] = round(watts(ch.get("w"), tare), 1) if ch.get("w") is not None else None
    return out
