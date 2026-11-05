"""DC meters (PZEM-017): type detected at pairing, own price, DC display / CSV; read + send intervals."""
from app.extensions import db
from app.models import Device, now
from app.services import devconfig, ingest, settings

from .conftest import FakeConn
from .test_pairing import _code


def _pair_dc(client, code, device_id="DCM23H4R"):
    return client.post("/api/v1/pair", json={"code": code, "device_id": device_id, "fw": "0.6.0", "proto": 1,
                                             "caps": {"kind": "dc", "sensor": "pzem017", "channels": 1,
                                                      "amp_rating": 100}})


def test_dc_meter_detected_at_pairing(app, admin):
    assert _pair_dc(admin, _code(admin)).status_code == 200
    with app.app_context():
        d = Device.query.filter_by(device_id="DCM23H4R").one()
        assert d.kind == "dc" and d.sensor_name == "DC Power Sensor"
    html = admin.get("/").get_data(as_text=True)
    assert ">DC</span>" in html


def test_separate_ac_and_dc_price(app, admin):
    r = admin.post("/settings/server", data={"timezone": "UTC", "raw_retention_days": "90",
                                             "currency": "MYR", "price_per_kwh": "0.21",
                                             "dc_currency": "MYR", "dc_price_per_kwh": "0.35"})
    assert r.status_code in (200, 302)
    assert settings.price("ac") == ("MYR", 0.21)
    assert settings.price("dc") == ("MYR", 0.35)


def test_dc_session_uses_dc_price_and_csv_has_no_pf(app, admin):
    with app.app_context():
        settings.set_many({"currency": "MYR", "price_per_kwh": "0.21", "dc_currency": "MYR", "dc_price_per_kwh": "0.35"})
    _pair_dc(admin, _code(admin))
    with app.app_context():
        d = Device.query.filter_by(device_id="DCM23H4R").one()
        conn = FakeConn()
        t = now() - 100
        ingest.handle(conn, d, {"t": "evt", "seq": 1, "ts": t, "tv": 1, "e": "session_start", "sid": 1, "max": 0})
        ingest.handle(conn, d, {"t": "tel", "items": [{"seq": 2, "ts": t + 10, "tv": 1,
                                                       "ch": [{"i": 0, "v": 48.12, "a": 210.5, "w": 10129.3, "e": 5.0}]}]})
        from app.models import Session
        assert Session.query.filter_by(device_fk=d.id, sid=1).one().price_per_kwh == 0.35
        assert d.last_reading["ch"][0]["a"] == 210.5  # above the old 200 A limit (300 A shunt)
        csv = admin.get(f"/meters/{d.id}/export.csv?range=1d").get_data(as_text=True)
        header = csv.splitlines()[0]
        assert "PF" not in header and "Hz" not in header and "V" in header


def test_read_and_send_interval_config(app):
    with app.app_context():
        d = Device(device_id="X1", name="x", caps={"channels": 1}, cfg={}, pair_gen=1)
        base = {"sess_mode": "none", "daily_at": "00:00"}
        cfg, errors = devconfig.parse_form({**base, "read_s": "10", "report_s": "60"}, d)
        assert not errors
        doc = devconfig.to_device(cfg, 1)["cfg"]
        assert doc["read_s"] == 10 and doc["report_s"] == 60
        _, errors = devconfig.parse_form({**base, "read_s": "120", "report_s": "60"}, d)
        assert any("Read data every" in e for e in errors)
        cfg, errors = devconfig.parse_form({**base, "report_s": "30"}, d)  # no read_s: one reading per send
        assert not errors and cfg["read_s"] == 30
