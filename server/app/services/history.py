"""History queries for the Meter Info chart, stats and CSV export (docs/data-model.md)."""
from sqlalchemy import func

from ..models import Measurement, Rollup, now
from . import settings
from .rollup import energy_kwh

RANGES = {"1h": 3600, "1d": 86400, "1w": 7 * 86400, "1m": 30 * 86400}
RAW_MAX_SPAN = 48 * 3600
HOURLY_MAX_SPAN = 60 * 86400
MAX_POINTS = 1500


def first_ts(device_pk):
    a = Measurement.query.with_entities(func.min(Measurement.ts)).filter_by(device_fk=device_pk).scalar()
    b = Rollup.query.with_entities(func.min(Rollup.bucket_start)).filter_by(device_fk=device_pk).scalar()
    vals = [v for v in (a, b) if v is not None]
    return min(vals) if vals else None


def resolve_range(device_pk, rng, start=None, end=None):
    t = now()
    if rng in RANGES:
        return t - RANGES[rng], t
    if rng == "all":
        return (first_ts(device_pk) or t - 86400), t
    if start and end and start < end:
        return start, end
    return t - 86400, t


def source_for(start, end):
    span = end - start
    if span <= RAW_MAX_SPAN:
        return "raw"
    if span <= HOURLY_MAX_SPAN:
        return "h"
    return "d"


def _raw(device_pk, ch, start, end):
    return (Measurement.query
            .filter(Measurement.device_fk == device_pk, Measurement.ch == ch,
                    Measurement.ts >= start, Measurement.ts <= end)
            .order_by(Measurement.ts, Measurement.seq).all())


def _rollups(device_pk, ch, period, start, end):
    return (Rollup.query
            .filter(Rollup.device_fk == device_pk, Rollup.ch == ch, Rollup.period == period,
                    Rollup.bucket_start >= start - (start % (3600 if period == "h" else 86400)),
                    Rollup.bucket_start <= end)
            .order_by(Rollup.bucket_start).all())


def _r(x, nd=3):
    return None if x is None else round(x, nd)


def query(device_pk, ch, start, end):
    """Return {"source", "points": [...], "stats": {...}} for the chart and stats panel."""
    src = source_for(start, end)
    points = []
    stats = {}
    if src == "raw":
        rows = _raw(device_pk, ch, start, end)
        step = max(1, -(-len(rows) // MAX_POINTS))
        cum, last_e = 0.0, None
        for i in range(0, len(rows), step):
            chunk = rows[i:i + step]
            for r in chunk:
                if r.e is not None:
                    if last_e is not None and r.e >= last_e:
                        cum += r.e - last_e
                    last_e = r.e
            p = {"t": chunk[-1].ts, "kwh_cum": round(cum, 4)}
            for f in ("v", "a", "w"):
                vals = [getattr(r, f) for r in chunk if getattr(r, f) is not None]
                p[f] = _r(sum(vals) / len(vals)) if vals else None
                p[f + "_min"] = _r(min(vals)) if vals else None
                p[f + "_max"] = _r(max(vals)) if vals else None
            points.append(p)
        for f in ("v", "a", "w"):
            vals = [getattr(r, f) for r in rows if getattr(r, f) is not None]
            stats[f] = {"min": _r(min(vals)) if vals else None, "max": _r(max(vals)) if vals else None,
                        "avg": _r(sum(vals) / len(vals)) if vals else None}
        stats["kwh"] = _r(energy_kwh([r.e for r in rows]), 4)
        stats["n"] = len(rows)
    else:
        rows = _rollups(device_pk, ch, src, start, end)
        for r in rows:
            p = {"t": r.bucket_start}
            for f in ("v", "a", "w"):
                p[f] = _r(getattr(r, f + "_avg"))
                p[f + "_min"] = _r(getattr(r, f + "_min"))
                p[f + "_max"] = _r(getattr(r, f + "_max"))
            p["kwh"] = _r(r.kwh, 4)
            points.append(p)
        total_n = sum(r.n for r in rows)
        for f in ("v", "a", "w"):
            mins = [getattr(r, f + "_min") for r in rows if getattr(r, f + "_min") is not None]
            maxs = [getattr(r, f + "_max") for r in rows if getattr(r, f + "_max") is not None]
            wavg = [(getattr(r, f + "_avg"), r.n) for r in rows if getattr(r, f + "_avg") is not None]
            n = sum(c for _, c in wavg)
            stats[f] = {"min": _r(min(mins)) if mins else None, "max": _r(max(maxs)) if maxs else None,
                        "avg": _r(sum(v * c for v, c in wavg) / n) if n else None}
        stats["kwh"] = _r(sum(r.kwh for r in rows), 4)
        stats["n"] = total_n
    return {"source": src, "start": start, "end": end, "points": points, "stats": stats}


def csv_rows(device_pk, ch, start, end):
    """Yield (header, rows) for CSV export: raw data if still kept, otherwise roll-ups."""
    retention_start = now() - settings.get_int("raw_retention_days", 90) * 86400
    if start >= retention_start:
        rows = _raw(device_pk, ch, start, end)
        header = ["V", "A", "W", "PF", "Hz", "energy_kWh", "time_estimated"]
        return "raw", header, [(r.ts, [r.v, r.a, r.w, r.pf, r.hz, r.e, int(r.time_estimated)]) for r in rows]
    period = "h" if end - start <= 730 * 86400 else "d"
    rows = _rollups(device_pk, ch, period, start, end)
    header = ["samples", "V_min", "V_max", "V_avg", "A_min", "A_max", "A_avg",
              "W_min", "W_max", "W_avg", "PF_avg", "Hz_avg", "kWh"]
    return period, header, [(r.bucket_start, [r.n, r.v_min, r.v_max, r.v_avg, r.a_min, r.a_max, r.a_avg,
                                              r.w_min, r.w_max, r.w_avg, r.pf_avg, r.hz_avg, r.kwh])
                            for r in rows]
