"""Tare: subtract the meter's own consumption (ESP32 power supply + SSR on the measured line).

Raw values are always stored as measured; tared values are computed when displayed or exported.
The offsets are part of the meter config, so with "tare_limits" (default) the meter itself
uses tared values for its kWh limit and max current (firmware 0.3+). Voltage is never tared.
"""


def offsets(device):
    """(amps, watts) or None when no tare is set."""
    cfg = device.cfg or {}
    a = float(cfg.get("tare_a", device.tare_a or 0) or 0)
    w = float(cfg.get("tare_w", device.tare_w or 0) or 0)
    return (a, w) if (a or w) else None


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
