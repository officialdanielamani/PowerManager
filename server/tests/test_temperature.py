"""DS18B20 temperatures (firmware TEMP_ENABLE): storage, latest values, over-temperature, config, history."""
from app.extensions import db
from app.models import Device, Incident, Notification, Temperature, now
from app.services import devconfig, history, ingest

from .conftest import FakeConn
from .test_pairing import _code


def _pair_temp(admin):
    return admin.post("/api/v1/pair", json={"code": _code(admin), "device_id": "TMP00001", "fw": "0.6.6", "proto": 1,
                                            "caps": {"kind": "ac", "channels": 1, "relays": 1, "temp": 2, "tamper": False}})


def _tel(seq, ts, t=None):
    item = {"seq": seq, "ts": ts, "tv": 1, "ch": [{"i": 0, "v": 230, "a": 1, "w": 230, "e": 1.0}]}
    if t is not None:
        item["t"] = t
    return {"t": "tel", "items": [item]}


def test_temperatures_stored_and_shown(app, admin):
    assert _pair_temp(admin).status_code == 200
    with app.app_context():
        d = Device.query.filter_by(device_id="TMP00001").one()
        pk, t0 = d.id, now() - 100
        ingest.handle(FakeConn(), d, _tel(1, t0, ["24.5", None]))
        ingest.handle(FakeConn(), d, _tel(1, t0, ["24.5", None]))  # re-sent: stored once
        ingest.handle(FakeConn(), d, _tel(2, t0 + 10))             # no new round: latest values kept
        assert Temperature.query.filter_by(device_fk=pk).count() == 1
        assert d.last_reading["t"] == [24.5, None]
        ingest.handle(FakeConn(), d, _tel(3, t0 + 20, [26.0, 31.5]))
        db.session.commit()
    live = admin.get(f"/meters/{pk}/live").get_json()
    assert live["temps"] == [{"name": "T1", "c": 26.0}, {"name": "T2", "c": 31.5}]
    h = admin.get(f"/meters/{pk}/history?range=1h").get_json()["temps"]
    assert h["names"] == ["T1", "T2"] and h["stats"][0]["max"] == 26.0 and h["stats"][1]["avg"] == 31.5
    page = admin.get(f"/meters/{pk}").get_data(as_text=True)
    assert 'data-series="temp"' in page
    conf = admin.get(f"/meters/{pk}/configure").get_data(as_text=True)
    assert "Temperature Protection" in conf and "not fitted on this meter" in conf  # tamper: TAMPER_ENABLE 0


def test_over_temperature_event_and_config(app, admin):
    assert _pair_temp(admin).status_code == 200
    with app.app_context():
        d = Device.query.filter_by(device_id="TMP00001").one()
        cfg, errors = devconfig.parse_form({"report_s": "60", "read_s": "60", "sess_mode": "none", "daily_at": "00:00",
                                             "kwh_max_0": "0", "amp_max_0": "0", "temp_max": "70", "temp_action": "end"}, d)
        assert not errors
        doc = devconfig.to_device(cfg, 2)["cfg"]
        assert doc["temp"] == {"max": 70.0, "action": "end"}
        t = now() - 50
        ingest.handle(FakeConn(), d, {"t": "evt", "seq": 5, "ts": t, "tv": 1, "e": "temp_high",
                                      "i": 1, "c": "72.5", "max": 70, "action": "hold"})
        inc = Incident.query.filter_by(device_fk=d.id, type="temp").one()
        assert inc.ended_at is None and inc.detail["c"] == "72.5"
        assert Notification.query.filter(Notification.message.like("%T2 72.5 °C%")).count() >= 1
        ingest.handle(FakeConn(), d, {"t": "evt", "seq": 6, "ts": t + 30, "tv": 1, "e": "temp_ok", "i": 1, "c": "64.0"})
        assert Incident.query.filter_by(device_fk=d.id, type="temp").one().ended_at == t + 30
        assert history.temps(d.id, t - 10, t + 10) is None  # no readings in that range
