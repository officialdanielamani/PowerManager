"""History queries for the Meter Info chart, stats and CSV export (docs/data-model.md)."""
from sqlalchemy import func, text

from ..models import Measurement, Rollup, now
from . import settings
from .rollup import energy_kwh

RANGES = {"1h": 3600, "1d": 86400, "1w": 7 * 86400, "1m": 30 * 86400}
RAW_MAX_SPAN = 48 * 3600
HOURLY_MAX_SPAN = 60 * 86400
MAX_POINTS = 1500


def first_ts(device_pk):
    """Oldest data of a meter ("All time"). Per channel, so each MIN uses the (device, ch, ts) index
    instead of scanning every reading of the meter."""
    from ..models import Channel
    chans = {c.idx for c in Channel.query.filter_by(device_fk=device_pk)} | {0}
    chans |= {c for (c,) in Rollup.query.with_entities(Rollup.ch).filter_by(device_fk=device_pk, period="d").distinct()}
    vals = [Measurement.query.with_entities(func.min(Measurement.ts))
            .filter(Measurement.device_fk == device_pk, Measurement.ch == ch).scalar() for ch in sorted(chans)]
    vals.append(Rollup.query.with_entities(func.min(Rollup.bucket_start)).filter_by(device_fk=device_pk).scalar())
    vals = [v for v in vals if v is not None]
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
            .order_by(Measurement.ts, Measurement.ms, Measurement.seq).all())


def _rollups(device_pk, ch, period, start, end):
    return (Rollup.query
            .filter(Rollup.device_fk == device_pk, Rollup.ch == ch, Rollup.period == period,
                    Rollup.bucket_start >= start - (start % (3600 if period == "h" else 86400)),
                    Rollup.bucket_start <= end)
            .order_by(Rollup.bucket_start).all())


def _count(device_pk, ch, start, end):
    return (Measurement.query.with_entities(func.count())
            .filter(Measurement.device_fk == device_pk, Measurement.ch == ch,
                    Measurement.ts >= start, Measurement.ts <= end).scalar() or 0)


_BUCKET_SQL = text("""
    SELECT (ts - :s) / :bs AS b, COUNT(*), MAX(ts + COALESCE(ms, 0) / 1000.0),
           MIN(v), MAX(v), SUM(v), COUNT(v), MIN(a), MAX(a), SUM(a), COUNT(a), MIN(w), MAX(w), SUM(w), COUNT(w)
    FROM measurement WHERE device_fk = :d AND ch = :c AND ts >= :s AND ts <= :e
    GROUP BY b ORDER BY b""")

_BUCKET_ENERGY_SQL = text("""
    SELECT b, SUM(CASE WHEN de > 0 THEN de ELSE 0 END) FROM (
      SELECT (ts - :s) / :bs AS b, e - LAG(e) OVER (ORDER BY ts, COALESCE(ms, 0), seq) AS de
      FROM measurement WHERE device_fk = :d AND ch = :c AND ts >= :s AND ts <= :e AND e IS NOT NULL)
    GROUP BY b""")


def _raw_buckets(device_pk, ch, start, end):
    """Many raw readings (e.g. DRDY: ~860,000 per channel per day): averaged / min / max per time
    bucket in SQL, so at most MAX_POINTS points reach Python and the browser."""
    from ..extensions import db
    bs = max(1, -(-(end - start) // MAX_POINTS))  # bucket length in whole seconds
    args = {"d": device_pk, "c": ch, "s": start, "e": end, "bs": bs}
    energy = {b: de or 0.0 for b, de in db.session.execute(_BUCKET_ENERGY_SQL, args)}
    points, cum, n_total = [], 0.0, 0
    tot = {f: [None, None, 0.0, 0] for f in ("v", "a", "w")}  # min, max, sum, count
    for row in db.session.execute(_BUCKET_SQL, args):
        b, n, t = row[0], row[1], row[2]
        n_total += n
        cum += energy.get(b, 0.0)
        p = {"t": round(t, 3), "kwh_cum": round(cum, 4)}
        for k, f in enumerate(("v", "a", "w")):
            mn, mx, sm, cnt = row[3 + 4 * k:7 + 4 * k]
            p[f] = _r(sm / cnt) if cnt else None
            p[f + "_min"], p[f + "_max"] = _r(mn), _r(mx)
            if cnt:
                agg = tot[f]
                agg[0] = mn if agg[0] is None else min(agg[0], mn)
                agg[1] = mx if agg[1] is None else max(agg[1], mx)
                agg[2] += sm
                agg[3] += cnt
        points.append(p)
    stats = {f: {"min": _r(a[0]), "max": _r(a[1]), "avg": _r(a[2] / a[3]) if a[3] else None} for f, a in tot.items()}
    stats["kwh"] = _r(cum, 4)
    stats["n"] = n_total
    return points, stats


def _r(x, nd=3):
    return None if x is None else round(x, nd)


def query(device_pk, ch, start, end):
    """Return {"source", "points": [...], "stats": {...}} for the chart and stats panel."""
    src = source_for(start, end)
    points = []
    stats = {}
    if src == "raw" and _count(device_pk, ch, start, end) > MAX_POINTS:
        points, stats = _raw_buckets(device_pk, ch, start, end)
    elif src == "raw":
        rows = _raw(device_pk, ch, start, end)
        cum, last_e = 0.0, None
        for r in rows:
            if r.e is not None:
                if last_e is not None and r.e >= last_e:
                    cum += r.e - last_e
                last_e = r.e
            p = {"t": r.ts + (r.ms or 0) / 1000, "kwh_cum": round(cum, 4)}
            for f in ("v", "a", "w"):
                val = getattr(r, f)
                p[f] = p[f + "_min"] = p[f + "_max"] = _r(val)
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


_TEMP_SQL = text("""
    SELECT (ts - :s) / :bs AS b, idx, MAX(ts), AVG(c), MIN(c), MAX(c), AVG(rh)
    FROM temperature WHERE device_fk = :d AND ts >= :s AND ts <= :e AND c IS NOT NULL
    GROUP BY b, idx ORDER BY b, idx""")


def temps(device_pk, start, end):
    """Temperature sensors (T1, T2 ...) over a range for the chart: averaged per time bucket in SQL
    (at most MAX_POINTS points). None when the meter has no temperature readings in the range."""
    from ..extensions import db
    bs = max(1, -(-(end - start) // MAX_POINTS))
    rows = db.session.execute(_TEMP_SQL, {"d": device_pk, "s": start, "e": end, "bs": bs}).all()
    if not rows:
        return None
    n = max(r[1] for r in rows) + 1
    buckets, order = {}, []
    stats = [[None, None, 0.0, 0] for _ in range(n)]
    for b, idx, t, avg, mn, mx, rh in rows:
        if b not in buckets:
            buckets[b] = [t, [None] * n, [None] * n]
            order.append(b)
        buckets[b][0] = max(buckets[b][0], t)
        buckets[b][1][idx] = round(avg, 1)
        buckets[b][2][idx] = round(rh, 1) if rh is not None else None
        st = stats[idx]
        st[0] = mn if st[0] is None else min(st[0], mn)
        st[1] = mx if st[1] is None else max(st[1], mx)
        st[2] += avg
        st[3] += 1
    return {"names": [f"T{i + 1}" for i in range(n)], "t": [buckets[b][0] for b in order],
            "series": [[buckets[b][1][i] for b in order] for i in range(n)],
            "rh": [[buckets[b][2][i] for b in order] for i in range(n)],
            "has_rh": any(buckets[b][2][i] is not None for b in order for i in range(n)),
            "stats": [{"min": _r(s[0], 1), "max": _r(s[1], 1), "avg": _r(s[2] / s[3], 1) if s[3] else None}
                      for s in stats]}


def csv_rows(device_pk, ch, start, end):
    """Yield (header, rows) for CSV export: raw data if still kept, otherwise roll-ups."""
    retention_start = now() - settings.get_int("raw_retention_days", 90) * 86400
    if start >= retention_start:
        q = (Measurement.query
             .filter(Measurement.device_fk == device_pk, Measurement.ch == ch,
                     Measurement.ts >= start, Measurement.ts <= end)
             .order_by(Measurement.ts, Measurement.ms, Measurement.seq))
        header = ["V", "A", "W", "PF", "Hz", "energy_kWh", "time_estimated", "ms"]
        # A generator: rows are read in batches while the CSV is streamed (no full copy in memory).
        return "raw", header, ((r.ts, [r.v, r.a, r.w, r.pf, r.hz, r.e, int(r.time_estimated), r.ms])
                               for r in q.yield_per(2000))
    period = "h" if end - start <= 730 * 86400 else "d"
    rows = _rollups(device_pk, ch, period, start, end)
    header = ["samples", "V_min", "V_max", "V_avg", "A_min", "A_max", "A_avg",
              "W_min", "W_max", "W_avg", "PF_avg", "Hz_avg", "kWh"]
    return period, header, [(r.bucket_start, [r.n, r.v_min, r.v_max, r.v_avg, r.a_min, r.a_max, r.a_avg,
                                              r.w_min, r.w_max, r.w_avg, r.pf_avg, r.hz_avg, r.kwh])
                            for r in rows]


def apply_tare(data, tare):
    """Turn a query() result into tared values: current/power minus the offsets, energy minus
    the offset power over the time covered. Voltage, PF and Hz are unchanged."""
    from . import tare as t
    if not tare:
        return data
    pts = data["points"]
    bucket_s = {"h": 3600, "d": 86400}.get(data["source"])
    t0 = pts[0]["t"] if pts else data["start"]
    for p in pts:
        for f, fn in (("a", t.amps), ("w", t.watts)):
            for k in (f, f + "_min", f + "_max"):
                if p.get(k) is not None:
                    p[k] = _r(fn(p[k], tare))
        if p.get("kwh") is not None and bucket_s:
            p["kwh"] = _r(t.kwh(p["kwh"], bucket_s, tare), 4)
        if p.get("kwh_cum") is not None:
            p["kwh_cum"] = round(t.kwh(p["kwh_cum"], p["t"] - t0, tare), 4)
    st = data["stats"]
    for f, fn in (("a", t.amps), ("w", t.watts)):
        for k in ("min", "max", "avg"):
            if (st.get(f) or {}).get(k) is not None:
                st[f][k] = _r(fn(st[f][k], tare))
    if st.get("kwh") is not None:
        covered = (pts[-1]["t"] - pts[0]["t"]) if (pts and not bucket_s) else len(pts) * (bucket_s or 0)
        st["kwh"] = _r(t.kwh(st["kwh"], covered, tare), 4)
    data["tared"] = True
    return data


def _only_channels(header, rows, first, n, allowed):
    """Keep only the columns of the allowed channels (4 columns per channel from index `first`),
    and drop rows that have no value for any of them."""
    keep = [i for i in range(len(header)) if not first <= i < first + 4 * n or (i - first) // 4 in allowed]
    chan_cols = [k for k, i in enumerate(keep) if first <= i < first + 4 * n]

    def gen():
        for ts, vals in rows:
            v = [vals[i] for i in keep]
            if any(v[k] is not None for k in chan_cols):
                yield ts, v
    return [header[i] for i in keep], gen()


def csv_wide(device, start, end, channels=None):
    """Multi-channel meters: one CSV row per reading with every channel side by side.
    Returns (source, header, rows) with rows = [(ts, [values...])]; channel columns are named like
    the meter's channels ("CH1 Kitchen A", "CH1 Kitchen W", ...). channels: a set of channel indexes
    to keep (None = all)."""
    source, header, rows = _csv_wide(device, start, end)
    if channels is not None:
        from . import devconfig
        first = 3 if source == "raw" else 2
        header, rows = _only_channels(header, rows, first, devconfig.channel_count(device), channels)
    return source, header, rows


def _csv_wide(device, start, end):
    from . import devconfig
    n = devconfig.channel_count(device)
    names = [device.channel_name(i) for i in range(n)]
    retention_start = now() - settings.get_int("raw_retention_days", 90) * 86400
    if start >= retention_start:
        q = (Measurement.query
             .filter(Measurement.device_fk == device.id, Measurement.ts >= start, Measurement.ts <= end)
             .order_by(Measurement.ts, Measurement.ms, Measurement.seq, Measurement.ch))
        header = ["ms", "V", "Hz"] + [f"{nm} {col}" for nm in names for col in ("A", "W", "PF", "kWh total")] + ["time_estimated"]

        def gen():  # streamed: one reading at a time
            cur, key = None, None
            for r in q.yield_per(2000):
                k = (r.gen, r.seq)  # one reading = one seq (DRDY: one per measurement window)
                if k != key:
                    if cur is not None:
                        yield cur
                    key = k
                    cur = (r.ts, [r.ms, r.v, r.hz] + [None] * (4 * n) + [int(r.time_estimated)])
                if 0 <= r.ch < n:
                    base = 3 + 4 * r.ch
                    cur[1][base:base + 4] = [r.a, r.w, r.pf, r.e]
                    if cur[1][1] is None:
                        cur[1][1] = r.v
            if cur is not None:
                yield cur
        return "raw", header, gen()
    period = "h" if end - start <= 730 * 86400 else "d"
    buckets = {}
    for ch in range(n):
        for r in _rollups(device.id, ch, period, start, end):
            b = buckets.setdefault(r.bucket_start, [None, None] + [None] * (4 * n))
            if b[0] is None:
                b[0], b[1] = r.v_avg, r.hz_avg
            b[2 + 4 * ch:6 + 4 * ch] = [r.a_avg, r.w_avg, r.w_max, r.kwh]
    header = ["V_avg", "Hz_avg"] + [f"{nm} {col}" for nm in names for col in ("A_avg", "W_avg", "W_max", "kWh")]
    return period, header, [(t, buckets[t]) for t in sorted(buckets)]


def delete_range(device_pk, start, end):
    """Delete the power readings of a meter between start and end (inclusive) and fix the hourly / daily
    summaries: buckets fully inside the range go, buckets at the edges are rebuilt from the readings
    that remain (if those readings are still kept). Sessions and events are not touched.
    Returns the number of readings deleted."""
    from ..extensions import db
    from ..models import RollupDirty
    from .rollup import build_bucket
    n = (Measurement.query.filter(Measurement.device_fk == device_pk, Measurement.ts >= start, Measurement.ts <= end)
         .delete(synchronize_session=False))
    from ..models import Temperature
    (Temperature.query.filter(Temperature.device_fk == device_pk, Temperature.ts >= start, Temperature.ts <= end)
     .delete(synchronize_session=False))
    chans = [c for (c,) in db.session.query(Rollup.ch).filter(Rollup.device_fk == device_pk).distinct()]
    for period, span in (("h", 3600), ("d", 86400)):
        (Rollup.query.filter(Rollup.device_fk == device_pk, Rollup.period == period, Rollup.bucket_start >= start,
                             Rollup.bucket_start + span - 1 <= end).delete(synchronize_session=False))
        for edge in {start - start % span, end - end % span}:
            for ch in chans:
                has_raw = (Measurement.query.filter(Measurement.device_fk == device_pk, Measurement.ch == ch,
                                                    Measurement.ts >= edge, Measurement.ts < edge + span).first())
                if has_raw is not None:
                    build_bucket(device_pk, ch, period, edge)
    (RollupDirty.query.filter(RollupDirty.device_fk == device_pk, RollupDirty.bucket_start >= start,
                              RollupDirty.bucket_start <= end).delete(synchronize_session=False))
    return n

