import pytest

from app.extensions import db
from app.models import Device, now
from app.services import history, ingest, tare

from .conftest import FakeConn
from .test_access import _meter
from .test_ingest import tel


def test_tare_math():
    t = (0.02, 1.5)
    assert tare.amps(0.5, t) == pytest.approx(0.48) and tare.amps(0.01, t) == 0.0  # never negative
    assert tare.watts(100.0, t) == 98.5
    assert tare.kwh(1.0, 3600, t) == pytest.approx(0.9985)  # 1.5 W for 1 h = 1.5 Wh
    assert tare.kwh(0.001, 3600, t) == 0.0
    assert tare.reading({"v": 230, "a": 0.5, "w": 100, "e": 5}, t) == {"v": 230, "a": 0.48, "w": 98.5, "e": 5}
    assert tare.offsets(Device(tare_a=0, tare_w=0)) is None


def test_live_and_history_tared(app, admin):
    with app.app_context():
        pk = _meter("Dryer A", "AAAA1111")
        d = db.session.get(Device, pk)
        d.tare_a, d.tare_w = 0.01, 2.0
        conn = FakeConn()
        t0 = now() - 3600
        ingest.handle(conn, d, {"t": "evt", "seq": 1, "ts": t0, "tv": 1, "e": "session_start", "sid": 1})
        ingest.handle(conn, d, {"t": "state", "sess": {"sid": 1, "start": t0, "kwh": 0.5, "status": "active"},
                                "relay": [{"i": 0, "on": False}]})
        items = [tel(10 + i, t0 + i * 600, 1.0 + i * 0.05, w=100) for i in range(7)]
        ingest.handle(conn, d, {"t": "tel", "items": items})
        db.session.commit()
    m = admin.get(f"/meters/{pk}/live").get_json()
    assert m["w"] == 100.0 and m["w_tared"] == 98.0
    assert m["kwh"] == 0.5 and m["kwh_tared"] == pytest.approx(0.498, abs=1e-3)  # 2 W x ~1 h
    raw = admin.get(f"/meters/{pk}/history?range=1d").get_json()
    tar = admin.get(f"/meters/{pk}/history?range=1d&tared=1").get_json()
    assert raw["stats"]["w"]["avg"] == 100.0 and tar["stats"]["w"]["avg"] == 98.0
    assert tar["stats"]["kwh"] == pytest.approx(raw["stats"]["kwh"] - 0.002, abs=1e-4)  # 2 W over 1 h
    csv = admin.get(f"/meters/{pk}/export.csv?range=1d").get_data(as_text=True).splitlines()
    assert "A_tared (-0.01)" in csv[0] and csv[1].endswith(",98.0")


def test_tare_now_needs_relay_off_and_fresh_reading(app, admin):
    with app.app_context():
        pk = _meter("Dryer A", "AAAA1111")
        d = db.session.get(Device, pk)
        d.last_state = {"relay": [{"i": 0, "on": True}]}
        d.last_reading = {"ts": now(), "ch": [{"i": 0, "a": 0.012, "w": 1.6}]}
        db.session.commit()
    r = admin.post(f"/meters/{pk}/action", json={"action": "tare_now"})
    assert r.status_code == 409 and r.get_json()["error"] == "relay_on"
    with app.app_context():
        d = db.session.get(Device, pk)
        d.last_state = {"relay": [{"i": 0, "on": False}]}
        db.session.commit()
    r = admin.post(f"/meters/{pk}/action", json={"action": "tare_now"}).get_json()
    assert r["ok"] and r["a"] == 0.012 and r["w"] == 1.6
