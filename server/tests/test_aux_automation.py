"""AUX relay automation (both firmwares): schedule / event / override, config to the meter, time zone."""
from app.extensions import db
from app.models import Command, Device
from app.services import auxcfg, devconfig, ingest, settings

from .conftest import FakeConn
from .test_multichannel import CAPS, _pair_mc
from .test_pairing import _code


def _form(**kw):
    base = {"name": "Panel", "report_s": "60", "read_s": "60", "sess_mode": "none", "daily_at": "00:00", "price_per_kwh": "0.25"}
    base.update(kw)
    return base


def _mc_with_aux(app, admin):
    caps = dict(CAPS, aux=3, temp=2)
    r = admin.post("/api/v1/pair", json={"code": _code(admin), "device_id": "MCAUX777", "fw": "0.8.1", "proto": 1, "caps": caps})
    assert r.status_code == 200
    with app.app_context():
        return Device.query.filter_by(device_id="MCAUX777").one().id


def test_schedule_and_event_config(app, admin):
    pk = _mc_with_aux(app, admin)
    with app.app_context():
        settings.set_many({"timezone": "Asia/Kuala_Lumpur"})
    form = _form(**{
        "aux_name_0": "Desk Lamp", "aux_mode_0": "schedule",
        "aux_s0_use_0": "on", "aux_s0_on_0": "on", "aux_s0_start_0": "18:00", "aux_s0_end_0": "23:59",
        "aux_s0_days_0": [str(d) for d in range(7)],
        "aux_s1_use_0": "on", "aux_s1_on_0": "on", "aux_s1_start_0": "01:00", "aux_s1_end_0": "02:00", "aux_s1_days_0": ["5"],
        "aux_name_1": "Exhaust Fan", "aux_mode_1": "event", "aux_ev_src_1": "temp", "aux_ev_idx_1": "1",
        "aux_ev_cmp_1": "over", "aux_ev_value_1": "50", "aux_ev_on_1": "on",
        "aux_name_2": "Pump", "aux_mode_2": "manual"})
    r = admin.post(f"/meters/{pk}/configure", data=form)
    assert r.status_code in (200, 302)
    with app.app_context():
        d = db.session.get(Device, pk)
        doc = devconfig.to_device(d.cfg, 1)["cfg"]
        assert doc["tz_min"] == 480
        a0, a1, a2 = doc["aux"]
        assert a0["mode"] == "schedule" and a0["sched"] == [{"on": True, "start": 1080, "end": 1439, "days": 127},
                                                             {"on": True, "start": 60, "end": 120, "days": 32}]
        assert a0["override"] is False and a0["else"] == "off"
        assert a1["event"] == {"src": "temp", "idx": 1, "cmp": "over", "value": 50.0, "on": True}
        assert a2["mode"] == "manual"
        assert auxcfg.describe(d.cfg["aux"][1], d) == "Event: ON while Temperature T2 ≥ 50.0 °C"
        assert "Every day 18:00-23:59" in auxcfg.describe(d.cfg["aux"][0], d)
    # override not enabled: the buttons are refused for the automated relays, fine for the manual one
    r = admin.post(f"/meters/{pk}/action", json={"action": "aux_on", "i": 1})
    assert r.status_code == 409 and r.get_json()["error"] == "automation"
    assert admin.post(f"/meters/{pk}/action", json={"action": "aux_on", "i": 2}).get_json()["ok"]
    page = admin.get(f"/meters/{pk}/configure").get_data(as_text=True)
    assert "Exhaust Fan" in page and 'name="aux_s0_start_0" value="18:00"' in page


def test_schedule_errors(app, admin):
    pk = _mc_with_aux(app, admin)
    r = admin.post(f"/meters/{pk}/configure", data=_form(**{"aux_mode_0": "schedule", "aux_s0_use_0": "on",
                                                              "aux_s0_start_0": "18:00", "aux_s0_end_0": "23:00"}))
    html = r.get_data(as_text=True)
    assert "AUX1 schedule 1: pick at least one day" in html


def test_pzem_meter_gets_aux_relays(app, admin):
    r = admin.post("/api/v1/pair", json={"code": _code(admin), "device_id": "PZAUX001", "fw": "0.6.7", "proto": 1,
                                         "caps": {"kind": "ac", "channels": 1, "relays": 1, "aux": 3}})
    assert r.status_code == 200
    with app.app_context():
        d = Device.query.filter_by(device_id="PZAUX001").one()
        assert len(d.cfg["aux"]) == 3 and d.cfg["aux"][0]["mode"] == "manual"
        assert len(devconfig.to_device(d.cfg, 1)["cfg"]["aux"]) == 3
        ingest._apply_state(d, {"relay": [{"i": 0, "on": False}], "aux": [{"i": 0, "relay": 2, "on": True, "mode": "manual"}]})
        db.session.commit()
        pk = d.id
    m = admin.get(f"/meters/{pk}/live").get_json()
    assert m["aux"][0]["on"] and m["aux"][0]["relay"] == 2
    assert admin.post(f"/meters/{pk}/action", json={"action": "aux_off", "i": 0}).get_json()["ok"]
    with app.app_context():
        assert Command.query.filter_by(device_fk=pk, c="aux").count() == 1
    assert 'id="auxBox"' in admin.get(f"/meters/{pk}").get_data(as_text=True)


def test_timezone_change_repushes_schedules(app, admin):
    pk = _mc_with_aux(app, admin)
    admin.post(f"/meters/{pk}/configure", data=_form(**{"aux_mode_0": "schedule", "aux_s0_use_0": "on",
                                                          "aux_s0_start_0": "18:00", "aux_s0_end_0": "23:00",
                                                          "aux_s0_days_0": ["0"]}))
    with app.app_context():
        before = db.session.get(Device, pk).cfg_ver_wanted
    admin.post("/settings/server", data={"timezone": "Asia/Tokyo", "raw_retention_days": "90"})
    with app.app_context():
        assert db.session.get(Device, pk).cfg_ver_wanted == before + 1
        assert devconfig.to_device(db.session.get(Device, pk).cfg, 1)["cfg"]["tz_min"] == 540


def test_aux_dates_any_channel_and_flags(app, admin):
    """14 schedules, by date, 'any channel', tamper / Wi-Fi events; event
    sources follow the sensors the meter has."""
    pk = _mc_with_aux(app, admin)
    with app.app_context():
        d = db.session.get(Device, pk)
        assert "tamper" not in auxcfg.sources(d)  # no tamper input on this meter
        d.caps = dict(d.caps, tamper=True, temp=0)
        d.last_reading = {}
        db.session.commit()
        src = auxcfg.sources(d)
        assert "temp" not in src and "hum" not in src and {"tamper", "trip", "volt", "wifi", "server"} <= set(src)
    form = {"aux_mode_0": "schedule", "aux_mode_1": "event", "aux_ev_src_1": "watt", "aux_ev_idx_1": "254",
            "aux_ev_cmp_1": "over", "aux_ev_value_1": "100", "aux_ev_on_1": "on",
            "aux_mode_2": "event", "aux_ev_src_2": "server", "aux_ev_active_2": "active", "aux_ev_on_2": "off"}
    for k in range(13):
        form.update({f"aux_s{k}_use_0": "on", f"aux_s{k}_start_0": "08:00", f"aux_s{k}_end_0": "09:00",
                     f"aux_s{k}_days_0": [str(k % 7)]})
    form.update({"aux_s13_use_0": "on", "aux_s13_kind_0": "date", "aux_s13_date_0": "2026-12-25",
                 "aux_s13_yearly_0": "on", "aux_s13_start_0": "20:00", "aux_s13_end_0": "02:00"})
    r = admin.post(f"/meters/{pk}/configure", data=_form(**form))
    assert r.status_code in (200, 302) and "alert-danger" not in r.get_data(as_text=True)
    with app.app_context():
        d = db.session.get(Device, pk)
        a0, a1, a2 = devconfig.to_device(d.cfg, 1)["cfg"]["aux"]
        assert len(a0["sched"]) == 14 and a0["sched"][13] == {"on": True, "start": 1200, "end": 120, "date": [0, 12, 25]}
        assert a1["event"] == {"src": "watt", "idx": 254, "cmp": "over", "value": 100.0, "on": True}
        assert a2["event"] == {"src": "server", "idx": 0, "active": True, "on": False}
        assert "12-25 every year" in auxcfg.describe(d.cfg["aux"][0], d)
        assert "any channel" in auxcfg.describe(d.cfg["aux"][1], d)
        assert auxcfg.describe(d.cfg["aux"][2], d) == "Event: OFF while No server / internet"
    page = admin.get(f"/meters/{pk}/configure").get_data(as_text=True)
    assert 'name="aux_s13_date_0" value="2026-12-25"' in page and "Any channel" in page
    # temperature is not offered (no sensor), and choosing it anyway is refused
    assert '<option value="temp"' not in page
    r = admin.post(f"/meters/{pk}/configure", data=_form(**{"aux_mode_0": "event", "aux_ev_src_0": "temp",
                                                              "aux_ev_value_0": "30"}))
    assert r.status_code == 200 and "choose what the relay reacts to" in r.get_data(as_text=True)


def test_ups_input_and_battery_event(app, admin):
    """UPS signal input (firmware UPS_ENABLE): notification + historical event; UPS / battery % as AUX events
    only when the firmware has them."""
    from app.models import Incident, Notification
    from app.services import cfggen
    pk = _mc_with_aux(app, admin)
    with app.app_context():
        d = db.session.get(Device, pk)
        assert "ups" not in auxcfg.sources(d) and "bat" not in auxcfg.sources(d)
        d.caps = dict(d.caps, ups=True, battery=True)
        db.session.commit()
        assert {"ups", "bat"} <= set(auxcfg.sources(d))
        conn = FakeConn()
        ingest.handle(conn, d, {"t": "evt", "seq": 1, "ts": 1700000000, "e": "ups_on"})
        db.session.commit()
        inc = Incident.query.filter_by(device_fk=pk, type="ups").one()
        assert inc.ended_at is None
        assert any("UPS" in n.message for n in Notification.query.all())
        ingest.handle(conn, d, {"t": "evt", "seq": 2, "ts": 1700000600, "e": "ups_off"})
        db.session.commit()
        assert Incident.query.filter_by(device_fk=pk, type="ups").one().ended_at == 1700000600
    r = admin.post(f"/meters/{pk}/configure", data=_form(**{
        "aux_mode_0": "event", "aux_ev_src_0": "ups", "aux_ev_active_0": "active", "aux_ev_on_0": "on",
        "aux_mode_1": "event", "aux_ev_src_1": "bat", "aux_ev_cmp_1": "under", "aux_ev_value_1": "20", "aux_ev_on_1": "off"}))
    assert "alert-danger" not in r.get_data(as_text=True)
    with app.app_context():
        d = db.session.get(Device, pk)
        a0, a1, _ = devconfig.to_device(d.cfg, 1)["cfg"]["aux"]
        assert a0["event"] == {"src": "ups", "idx": 0, "active": True, "on": True}
        assert a1["event"] == {"src": "bat", "idx": 0, "cmp": "under", "value": 20.0, "on": False}
        assert auxcfg.describe(d.cfg["aux"][1], d) == "Event: OFF while Battery % ≤ 20.0 %"
    text, errors, _ = cfggen.generate({"meter": "pzem_ac", "pin_sda": 8, "pin_scl": 9, "pin_led": 48, "pin_pzem_rx": 5,
                                       "pin_pzem_tx": 6, "ups": True, "pin_ups": 2, "ups_level": "HIGH"})
    assert not errors and "#define UPS_ENABLE 1" in text and "#define UPS_ACTIVE_LEVEL HIGH" in text
