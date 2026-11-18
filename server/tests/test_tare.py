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
    assert tare.offsets(Device(calib={})) is None and tare.offsets(Device(calib={"0": {"tare": {"a": 0.1, "w": 5}}})) == (0.1, 5.0)
    assert tare.offsets(Device(calib={"1": {"tare": {"a": 0.1, "w": 5}}}), 1) == (0.1, 5.0) and tare.offsets(Device(calib={"1": {"tare": {"a": 1, "w": 1}}})) is None
    d = Device(calib={"0": {"tare": {"a": 0.1, "w": 5}}, "2": {"tare": {"a": 0.3, "w": 10}}, "x": {}})
    assert tare.any_tare(d) and tare.total(d, [0, 1, 2]) == pytest.approx((0.4, 15.0)) and tare.total(d, [1]) is None


def test_live_and_history_tared(app, admin):
    with app.app_context():
        pk = _meter("Dryer A", "AAAA1111")
        d = db.session.get(Device, pk)
        d.calib = {"0": {"tare": {"a": 0.01, "w": 2.0}}}
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


def test_meter_reported_tared_kwh_wins(app, admin):
    """Firmware 0.3+ sends its own tared session kWh (the value its limit uses)."""
    with app.app_context():
        pk = _meter("Dryer A", "AAAA1111")
        d = db.session.get(Device, pk)
        d.calib = {"0": {"tare": {"a": 0.0, "w": 2.0}}}
        ingest.handle(FakeConn(), d, {"t": "tel", "items": [{**tel(5, now(), 3.0), "s": {"sid": 1, "kwh": 0.8, "kwh_t": 0.75}}]})
        db.session.commit()
    m = admin.get(f"/meters/{pk}/live").get_json()
    assert m["kwh"] == 0.8 and m["kwh_tared"] == 0.75


def test_tare_goes_to_the_meter_config(app):
    from app.services import devconfig
    with app.app_context():
        cfg, errors = devconfig.parse_form({"report_s": "60", "sess_mode": "none", "daily_at": "00:00", "tare_limits": "on"}, Device(caps={}))
        calib = {"0": {"zero": {"a": 0.004, "w": 0.5}, "tare": {"a": 0.012, "w": 1.6}}}
        doc = devconfig.to_device(cfg, 1, calib)["cfg"]
        assert not errors and doc["tare_limits"] is True and "tare" not in doc
        assert doc["ch"][0]["zero"] == {"a": 0.004, "w": 0.5} and doc["ch"][0]["tare"] == {"a": 0.012, "w": 1.6}
        assert "zero" not in devconfig.to_device(cfg, 1, {})["cfg"]["ch"][0]       # nothing set = nothing sent
        off, _ = devconfig.parse_form({"report_s": "60", "sess_mode": "none", "daily_at": "00:00"}, Device(caps={}))
        assert devconfig.to_device(off, 1)["cfg"]["tare_limits"] is False
