"""Hourly/daily roll-ups built from raw measurements, and retention cleanup."""
from sqlalchemy import text

from ..extensions import db
from ..models import Measurement, Rollup, RollupDirty, Temperature, now
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


_AGG_SQL = text("""
    SELECT COUNT(*), MIN(v), MAX(v), AVG(v), MIN(a), MAX(a), AVG(a), MIN(w), MAX(w), AVG(w), AVG(pf), AVG(hz)
    FROM measurement WHERE device_fk = :d AND ch = :c AND ts >= :s AND ts < :e""")

# Energy = sum of the positive steps of the meter's kWh register (resets / rollovers ignored), in SQL.
_ENERGY_SQL = text("""
    SELECT COALESCE(SUM(CASE WHEN de > 0 THEN de ELSE 0 END), 0) FROM (
      SELECT e - LAG(e) OVER (ORDER BY ts, COALESCE(ms, 0), seq) AS de
      FROM measurement WHERE device_fk = :d AND ch = :c AND ts >= :s AND ts < :e AND e IS NOT NULL)""")


def energy_between(device_pk, ch, start, end):
    """kWh used between start and end (end exclusive), counting the step from the reading just before."""
    prev = (Measurement.query
            .filter(Measurement.device_fk == device_pk, Measurement.ch == ch,
                    Measurement.ts < start, Measurement.ts >= start - PREV_GAP_S,
                    Measurement.e.isnot(None))
            .order_by(Measurement.ts.desc()).first())
    first = (Measurement.query
             .filter(Measurement.device_fk == device_pk, Measurement.ch == ch,
                     Measurement.ts >= start, Measurement.ts < end, Measurement.e.isnot(None))
             .order_by(Measurement.ts, Measurement.ms, Measurement.seq).first())
    inner = db.session.execute(_ENERGY_SQL, {"d": device_pk, "c": ch, "s": start, "e": end}).scalar() or 0.0
    lead = first.e - prev.e if prev is not None and first is not None and first.e >= prev.e else 0.0
    return inner + lead


def build_bucket(device_pk, ch, period, start):
    """(Re)build one roll-up. Hourly ones are aggregated in SQL from the raw readings (no rows are
    loaded into Python, so a DRDY hour of 36,000 readings is cheap); daily ones from the hourly
    roll-ups of that day when there are any (24 rows instead of up to ~860,000 readings)."""
    end = start + SPAN[period]
    key = (device_pk, ch, period, start)
    roll = db.session.get(Rollup, key)
    if period == "d" and _daily_from_hourly(roll, device_pk, ch, start, end):
        return
    agg = db.session.execute(_AGG_SQL, {"d": device_pk, "c": ch, "s": start, "e": end}).one()
    if not agg[0]:
        if roll is not None:
            db.session.delete(roll)
        return
    if roll is None:
        roll = Rollup(device_fk=device_pk, ch=ch, period=period, bucket_start=start)
        db.session.add(roll)
    (roll.n, roll.v_min, roll.v_max, roll.v_avg, roll.a_min, roll.a_max, roll.a_avg,
     roll.w_min, roll.w_max, roll.w_avg, roll.pf_avg, roll.hz_avg) = agg
    roll.kwh = energy_between(device_pk, ch, start, end)


def _daily_from_hourly(roll, device_pk, ch, start, end):
    hours = (Rollup.query.filter(Rollup.device_fk == device_pk, Rollup.ch == ch, Rollup.period == "h",
                                 Rollup.bucket_start >= start, Rollup.bucket_start < end).all())
    hours = [h for h in hours if h.n]
    if not hours:
        return False
    if roll is None:
        roll = Rollup(device_fk=device_pk, ch=ch, period="d", bucket_start=start)
        db.session.add(roll)
    n = sum(h.n for h in hours)

    def wavg(attr):
        vals = [(getattr(h, attr), h.n) for h in hours if getattr(h, attr) is not None]
        c = sum(k for _, k in vals)
        return sum(v * k for v, k in vals) / c if c else None

    def ext(fn, attr):
        vals = [getattr(h, attr) for h in hours if getattr(h, attr) is not None]
        return fn(vals) if vals else None

    roll.n = n
    for f in ("v", "a", "w"):
        setattr(roll, f + "_min", ext(min, f + "_min"))
        setattr(roll, f + "_max", ext(max, f + "_max"))
        setattr(roll, f + "_avg", wavg(f + "_avg"))
    roll.pf_avg, roll.hz_avg = wavg("pf_avg"), wavg("hz_avg")
    roll.kwh = sum(h.kwh or 0 for h in hours)
    return True


def process_dirty(limit=500):
    # Hourly before daily: a daily roll-up is built from the hourly ones of its day.
    dirty = RollupDirty.query.order_by(RollupDirty.period.desc(), RollupDirty.bucket_start).limit(limit).all()
    for d in dirty:
        build_bucket(d.device_fk, d.ch, d.period, d.bucket_start)
        db.session.delete(d)
    db.session.commit()
    return len(dirty)


def retention():
    days = max(1, settings.get_int("raw_retention_days", 90))
    cutoff = now() - days * 86400
    Measurement.query.filter(Measurement.ts < cutoff).delete(synchronize_session=False)
    Temperature.query.filter(Temperature.ts < cutoff).delete(synchronize_session=False)  # kept like raw readings
    Rollup.query.filter(Rollup.period == "h", Rollup.bucket_start < now() - 730 * 86400) \
        .delete(synchronize_session=False)
    db.session.commit()
