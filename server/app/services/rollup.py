"""Hourly/daily roll-ups built from raw measurements, and retention cleanup."""
from ..extensions import db
from ..models import Measurement, Rollup, RollupDirty, now
from . import settings

SPAN = {"h": 3600, "d": 86400}
PREV_GAP_S = 7200  # use the reading just before a bucket for its first energy delta


def energy_kwh(e_values, prev_e=None):
    """Sum positive deltas of the PZEM energy register (ignores resets/rollovers)."""
    total = 0.0
    last = prev_e
    for e in e_values:
        if e is None:
            continue
        if last is not None and e >= last:
            total += e - last
        last = e
    return total


def _stats(rows, attr):
    vals = [getattr(r, attr) for r in rows if getattr(r, attr) is not None]
    if not vals:
        return None, None, None
    return min(vals), max(vals), sum(vals) / len(vals)


def build_bucket(device_pk, ch, period, start):
    end = start + SPAN[period]
    rows = (Measurement.query
            .filter(Measurement.device_fk == device_pk, Measurement.ch == ch,
                    Measurement.ts >= start, Measurement.ts < end)
            .order_by(Measurement.ts, Measurement.seq).all())
    key = (device_pk, ch, period, start)
    roll = db.session.get(Rollup, key)
    if not rows:
        if roll is not None:
            db.session.delete(roll)
        return
    if roll is None:
        roll = Rollup(device_fk=device_pk, ch=ch, period=period, bucket_start=start)
        db.session.add(roll)
    prev = (Measurement.query
            .filter(Measurement.device_fk == device_pk, Measurement.ch == ch,
                    Measurement.ts < start, Measurement.ts >= start - PREV_GAP_S,
                    Measurement.e.isnot(None))
            .order_by(Measurement.ts.desc()).first())
    roll.n = len(rows)
    roll.v_min, roll.v_max, roll.v_avg = _stats(rows, "v")
    roll.a_min, roll.a_max, roll.a_avg = _stats(rows, "a")
    roll.w_min, roll.w_max, roll.w_avg = _stats(rows, "w")
    roll.pf_avg = _stats(rows, "pf")[2]
    roll.hz_avg = _stats(rows, "hz")[2]
    roll.kwh = energy_kwh([r.e for r in rows], prev.e if prev else None)


def process_dirty(limit=500):
    dirty = RollupDirty.query.limit(limit).all()
    for d in dirty:
        build_bucket(d.device_fk, d.ch, d.period, d.bucket_start)
        db.session.delete(d)
    db.session.commit()
    return len(dirty)


def retention():
    days = max(1, settings.get_int("raw_retention_days", 90))
    cutoff = now() - days * 86400
    Measurement.query.filter(Measurement.ts < cutoff).delete(synchronize_session=False)
    Rollup.query.filter(Rollup.period == "h", Rollup.bucket_start < now() - 730 * 86400) \
        .delete(synchronize_session=False)
    db.session.commit()
