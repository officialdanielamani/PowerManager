import pytest

from app.extensions import db
from app.models import Device, Incident, Measurement, now
from app.routes.device_api import ensure_device_defaults
from app.services import devconfig, history, incidents, ingest, rollup, settings

from .conftest import FakeConn


@pytest.fixture()
def device(app):
    with app.app_context():
        d = Device(device_id="MTR1", name="Dryer", caps={"channels": 1}, cfg={}, pair_gen=1)
        db.session.add(d)
        db.session.flush()
        ensure_device_defaults(d, reset=True)
        db.session.commit()
        yield d


def tel(seq, ts, e, w=100.0):
    return {"seq": seq, "ts": ts, "tv": 1, "ch": [{"i": 0, "v": 230.0, "a": w / 230, "w": w, "pf": 1, "hz": 50, "e": e}]}


def test_telemetry_dedup_and_ack(app, device):
    conn = FakeConn()
    t = now() - 100
    msg = {"t": "tel", "items": [tel(1, t, 1.0), tel(2, t + 10, 1.1)]}
    ingest.handle(conn, device, msg)
    ingest.handle(conn, device, msg)  # resent after a lost ack
    assert Measurement.query.count() == 2
    assert conn.sent[-1]["t"] == "ack" and conn.sent[-1]["last_seq"] == 2


def test_time_estimation_from_uptime(app, device):
    conn = FakeConn()
    ingest.handle(conn, device, {"t": "hello", "up": 500, "boot": 3, "cfg_ver": 1})
    ingest.handle(conn, device, {"t": "tel", "items": [{"seq": 5, "ts": 400, "tv": 0, "boot": 3,
                                                        "ch": [{"i": 0, "w": 1}]}]})
    m = Measurement.query.one()
    assert m.time_estimated and abs(m.ts - (now() - 100)) <= 2


def test_incidents_from_events(app, device):
    conn = FakeConn()
    t = now() - 60
    ingest.handle(conn, device, {"t": "evt", "seq": 1, "ts": t, "tv": 1, "e": "overcurrent", "ch": 0, "val": 11.5, "limit": 10.2})
    ingest.handle(conn, device, {"t": "evt", "seq": 2, "ts": t + 30, "tv": 1, "e": "trip_reset", "src": "local", "by": "admin"})
    ingest.handle(conn, device, {"t": "evt", "seq": 3, "ts": t + 5, "tv": 1, "e": "tamper_open"})
    inc = Incident.query.filter_by(type="trip").one()
    assert inc.duration_s == 30 and inc.detail["reset_src"] == "local" and inc.detail["a"] == 11.5
    assert Incident.query.filter_by(type="tamper", ended_at=None).count() == 1


def test_state_sync_then_late_event_fixes_times(app, device):
    """Reconnect: hello says tamper closed before the buffered tamper_close event arrives."""
    conn = FakeConn()
    t = now() - 300
    ingest.handle(conn, device, {"t": "evt", "seq": 1, "ts": t, "tv": 1, "e": "tamper_open"})
    ingest.handle(conn, device, {"t": "hello", "up": 10, "cfg_ver": 1, "state": {"tamper": False, "ac": False}})
    inc = Incident.query.filter_by(type="tamper").one()
    assert inc.detail.get("closed_by") == "state_sync"
    ingest.handle(conn, device, {"t": "evt", "seq": 2, "ts": t + 60, "tv": 1, "e": "tamper_close"})
    ingest.handle(conn, device, {"t": "evt", "seq": 3, "ts": t + 20, "tv": 1, "e": "ac_lost"})
    inc = db.session.get(Incident, inc.id)
    assert inc.ended_at == t + 60 and inc.duration_s == 60
    power = Incident.query.filter_by(type="power").one()
    assert power.started_at == t + 20  # corrected from the state-sync time


def test_offline_incident_and_reconnect(app, device):
    conn = FakeConn()
    ingest.on_connect(device, conn)
    ingest.on_disconnect(device, "connection_lost")
    assert incidents.find_open(device.id, "offline") is not None
    ingest.on_connect(device, conn)
    assert incidents.find_open(device.id, "offline") is None


def test_hello_pushes_outdated_config(app, device):
    conn = FakeConn()
    device.cfg_ver_wanted = 3
    db.session.commit()
    ingest.handle(conn, device, {"t": "hello", "up": 1, "cfg_ver": 2})
    assert [m["t"] for m in conn.sent] == ["welcome", "cfg"]
    ingest.handle(conn, device, {"t": "cfg_ack", "ver": 3, "ok": True})
    assert device.synced


def test_daily_reset_converted_to_utc(app):
    with app.app_context():
        settings.set_many({"timezone": "Asia/Kuala_Lumpur"})
        cfg = devconfig.default_cfg(1)
        cfg.update(sess_mode="daily", daily_at="01:00", auto=True)
        doc = devconfig.to_device(cfg, 2)
    assert doc["cfg"]["sess"] == {"mode": "daily", "auto": True, "daily_utc_min": 17 * 60}


def test_rollups_and_history(app, device):
    conn = FakeConn()
    base = now() - now() % 3600 - 7200
    items = [tel(i + 1, base + i * 60, 1.0 + i * 0.01, w=600) for i in range(120)]
    ingest.handle(conn, device, {"t": "tel", "items": items[:100]})
    ingest.handle(conn, device, {"t": "tel", "items": items[100:]})
    while rollup.process_dirty():
        pass
    q = history.query(device.id, 0, base, base + 7200)
    assert q["source"] == "raw" and q["stats"]["kwh"] == pytest.approx(1.19, abs=1e-6)
    q = history.query(device.id, 0, base - 3 * 86400, base + 7200)
    assert q["source"] == "h" and q["stats"]["n"] == 120
    assert q["stats"]["kwh"] == pytest.approx(1.19, abs=1e-6)


def test_energy_ignores_resets():
    assert rollup.energy_kwh([5.0, 5.5, 0.1, 0.4], prev_e=4.9) == pytest.approx(0.9)
