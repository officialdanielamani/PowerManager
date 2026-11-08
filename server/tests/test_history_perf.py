"""History with many raw readings (DRDY): SQL bucketing / roll-ups give the same numbers as row by row."""
import random

from app.extensions import db
from app.models import Measurement, Rollup
from app.services import history, rollup
from app.services.rollup import energy_kwh

from .test_ingest import device  # noqa: F401 (fixture)


def _fill(device, start, n, step_ms=100):
    random.seed(1)
    rows, e = [], 10.0
    for k in range(n):
        off = k * step_ms
        e += 0.00005
        if k == n // 2:
            e = 0.0  # register reset: ignored, not a negative step
        rows.append(dict(device_fk=device.id, ch=0, gen=1, seq=-(k + 1), ts=start + off // 1000, ms=off % 1000,
                         v=230 + random.random(), a=random.random() * 10, w=random.random() * 2000,
                         e=round(e, 5) if k % 10 == 9 else None, time_estimated=False))
    db.session.execute(Measurement.__table__.insert(), rows)
    db.session.commit()
    return [type("R", (), r) for r in rows]


def test_raw_buckets_match_row_by_row(app, device):
    start = 1_700_000_000
    rows = _fill(device, start, 20_000)  # 2000 s of 100 ms windows
    end = start + 2000
    data = history.query(device.id, 0, start, end)
    assert data["source"] == "raw" and len(data["points"]) <= history.MAX_POINTS
    st = data["stats"]
    ws = [r.w for r in rows]
    assert st["n"] == 20_000
    assert st["w"]["min"] == round(min(ws), 3) and st["w"]["max"] == round(max(ws), 3)
    assert abs(st["w"]["avg"] - sum(ws) / len(ws)) < 0.01
    assert abs(st["kwh"] - energy_kwh([r.e for r in rows])) < 1e-4
    assert data["points"][-1]["kwh_cum"] == st["kwh"]


def test_rollups_sql_and_daily_from_hourly(app, device):
    start = 1_700_006_400 - 1_700_006_400 % 86400  # a day start
    rows = _fill(device, start + 3000, 50_000, step_ms=200)  # ~2.8 h across 3-4 hours
    for h in range(0, 5):
        rollup.build_bucket(device.id, 0, "h", start + h * 3600)
    rollup.build_bucket(device.id, 0, "d", start)
    db.session.commit()
    day = db.session.get(Rollup, (device.id, 0, "d", start))
    ws = [r.w for r in rows]
    assert day.n == 50_000 and abs(day.w_avg - sum(ws) / len(ws)) < 0.01
    assert day.w_max == max(ws) and day.w_min == min(ws)
    assert abs(day.kwh - energy_kwh([r.e for r in rows])) < 1e-6
    hours = Rollup.query.filter_by(device_fk=device.id, period="h").all()
    assert sum(h.n for h in hours) == 50_000
    assert history.first_ts(device.id) == start
