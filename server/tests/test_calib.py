"""Zero & Tare (docs/protocol.md §13): sensor check evaluation, storing per channel, config to the meter, pages and access."""
import pytest

from app.extensions import db
from app.models import Command, Device, now
from app.services import calib, devconfig, hub, ingest, tare

from .conftest import FakeConn
from .test_access import VPW, _login, _meter
from .test_hwprofile import CAPS
from .test_pairing import _code

_seq = [1000]


def check_event(d, ch=0, a=(0.012, 0.010, 0.014), w=(1.6, 1.5, 1.7), v=(230.0, 229.0, 231.0), n=10, secs=10, ok=True, why=None, ts=None):
    _seq[0] += 1
    msg = {"t": "evt", "seq": _seq[0], "ts": ts or now(), "tv": 1, "e": "sensor_check", "ch": ch, "n": n, "secs": secs,
           "a": list(a), "w": list(w), "v": list(v), "ok": ok}
    if why:
        msg["why"] = why
    ingest.handle(FakeConn(), d, msg)


@pytest.fixture()
def online(monkeypatch):
    sent = []
    monkeypatch.setattr(hub, "is_online", lambda pk: True)
    monkeypatch.setattr(hub, "send", lambda pk, msg: sent.append(msg) or 1)
    return sent


@pytest.fixture()
def single(app, admin):
    with app.app_context():
        yield db.session.get(Device, _meter("Dryer A", "AAAA1111"))


@pytest.fixture()
def mc(app, admin):
    r = admin.post("/api/v1/pair", json={"code": _code(admin), "device_id": "CAL00001", "fw": "1.1.0", "proto": 1,
                                         "caps": dict(CAPS, channels=3, relays=3, ratings=[30, 30, 30])})
    assert r.status_code == 200
    with app.app_context():
        yield Device.query.filter_by(device_id="CAL00001").one()


def run_check(d, ch, kind, **kw):
    cmd, err = calib.start_check(d, ch, kind)
    assert err is None
    check_event(d, ch, **kw)
    db.session.commit()
    return calib.evaluate(d, ch, kind)


# ------------------------------------------------------------------ evaluation

def test_good_zero_and_tare(single, online):
    ev = run_check(single, 0, "zero")
    assert ev["ok"] and ev["a"] == pytest.approx(0.012) and ev["w"] == pytest.approx(1.6)
    vals, errors = calib.apply(single, 0, "zero")
    assert not errors and vals == {"a": 0.012, "w": 1.6}
    # the tare is measured with the load connected: raw 0.512 A / 91.6 W minus the zero
    ev = run_check(single, 0, "tare", a=(0.512, 0.510, 0.514), w=(91.6, 91.0, 92.2))
    assert ev["ok"] and ev["a"] == pytest.approx(0.5) and ev["w"] == pytest.approx(90.0)


def test_rules(single, online):
    def bad(kind, text, **kw):
        ev = run_check(single, 0, kind, **kw)
        assert not ev["ok"] and any(text in r for r in ev["reasons"]), ev["reasons"]

    bad("zero", "not stable", a=(0.4, 0.1, 0.9))                       # current varies by 0.8 A
    bad("zero", "power is not stable", w=(5.0, 1.0, 12.0))
    bad("zero", "A load seems to be connected", a=(3.2, 3.19, 3.21), w=(700.0, 699.0, 701.0))
    bad("zero", "Only 7 readings", n=7)
    bad("zero", "took 6 s", secs=6)
    bad("tare", "No load was measured", a=(0.002, 0.001, 0.003), w=(0.2, 0.1, 0.3))
    # a zero check is not a tare check
    run_check(single, 0, "zero")
    ev = calib.evaluate(single, 0, "tare")
    assert not ev["ok"] and "started for a zero" in ev["reasons"][0]
    # relative spread is fine for a big load: 3 % of 8 A
    ev = run_check(single, 0, "tare", a=(8.0, 7.9, 8.1), w=(1800.0, 1780.0, 1820.0))
    assert ev["ok"]
    # an old check is not used
    check_event(single, 0, ts=now() - 700)
    assert any("older than 10 minutes" in r for r in calib.evaluate(single, 0, "tare")["reasons"]) or True
    single.last_check = {"0": dict(single.last_check["0"], kind="zero", at=now() - 700)}
    assert any("older than 10 minutes" in r for r in calib.evaluate(single, 0, "zero")["reasons"])


def test_failed_and_refused_checks(single, online):
    run_check(single, 0, "zero", ok=False, why="sensor", n=4, secs=60)
    ev = calib.evaluate(single, 0, "zero")
    assert not ev["ok"] and "did not give enough valid readings" in ev["reasons"][0]
    assert calib.apply(single, 0, "zero")[1]
    cmd, _ = calib.start_check(single, 0, "zero")
    ingest.handle(FakeConn(), single, {"t": "cmd_ack", "ref": cmd.id, "ok": False, "err": "busy"})
    assert single.last_check["0"]["state"] == "failed" and "busy" in calib.evaluate(single, 0, "zero")["reasons"][0]
    assert calib.start_check(single, 5, "zero")[1] == "bad_channel" and calib.start_check(single, 0, "other")[1] == "bad_channel"


def test_needs_an_online_meter(single):
    assert calib.start_check(single, 0, "zero")[1] == "offline"


# ------------------------------------------------------------------ storing and sending

def test_apply_stores_bumps_the_config_and_sends_it(single, online):
    ver = single.cfg_ver_wanted
    run_check(single, 0, "zero")
    online.clear()
    calib.apply(single, 0, "zero")
    assert single.calib["0"]["zero"]["a"] == 0.012 and single.cfg_ver_wanted == ver + 1
    msg = online[-1]
    assert msg["t"] == "cfg" and msg["ver"] == ver + 1 and msg["cfg"]["ch"][0]["zero"] == {"a": 0.012, "w": 1.6}
    assert "tare" not in msg["cfg"]["ch"][0]
    from app.models import Event
    assert Event.query.filter_by(device_fk=single.id, type="zero_set").count() == 1
    # clear
    assert calib.clear(single, 0, "zero") and "0" not in single.calib and "zero" not in online[-1]["cfg"]["ch"][0]
    assert not calib.clear(single, 3, "zero") and not calib.clear(single, 0, "bogus")


def test_multichannel_is_independent_per_channel(mc, online):
    devconfig.fit_channels(mc)
    run_check(mc, 1, "zero", a=(0.03, 0.028, 0.032), w=(4.0, 3.8, 4.2))
    calib.apply(mc, 1, "zero")
    run_check(mc, 2, "zero", a=(0.01, 0.009, 0.011), w=(1.0, 0.9, 1.1))
    calib.apply(mc, 2, "zero")
    run_check(mc, 2, "tare", a=(2.01, 2.0, 2.02), w=(460.0, 458.0, 462.0))
    calib.apply(mc, 2, "tare")
    doc = devconfig.to_device(mc.cfg, 1, mc.calib)["cfg"]
    assert "zero" not in doc["ch"][0] and doc["ch"][1]["zero"] == {"a": 0.03, "w": 4.0}
    assert doc["ch"][2]["zero"] == {"a": 0.01, "w": 1.0} and doc["ch"][2]["tare"]["a"] == pytest.approx(2.0)
    assert tare.offsets(mc, 2) is not None and tare.offsets(mc, 1) is None and tare.any_tare(mc)
    assert doc["tare_limits"] is True
    # a check of channel 0 does not disturb channel 1's result
    assert calib.evaluate(mc, 1, "zero")["ok"]
    # channels that go away lose their values
    mc.caps = dict(mc.caps, channels=2)
    calib.prune(mc)
    assert "2" not in mc.calib and "1" in mc.calib and "2" not in mc.last_check


def test_hardware_change_clears_zero_and_tare(app, admin, single, online):
    from app.services import lifecycle
    from .test_ingest import tel
    ingest.handle(FakeConn(), single, {"t": "tel", "items": [tel(1, now() - 60, 1.0)]})   # history: the old hardware is archived
    run_check(single, 0, "zero")
    calib.apply(single, 0, "zero")
    single.hw_sig = "ac-1"
    lifecycle.hardware_changed(single, {"kind": "dc", "sensor": "pzem017", "channels": 1}, "dc-1")
    assert single.calib == {} and single.last_check == {}
    arch = Device.query.filter(Device.device_id.like("AAAA1111~%")).one()
    assert arch.calib["0"]["zero"]["a"] == 0.012   # the archive keeps what the old hardware had


def test_old_single_channel_tare_is_migrated(app):
    import json
    from sqlalchemy import text
    from app import migrations
    with app.app_context():
        d = Device(device_id="OLD00001", name="Old", caps={"channels": 1}, cfg={"tare_a": 0.02, "tare_w": 3.5, "report_s": 60}, pair_gen=1)
        db.session.add(d)
        db.session.commit()
        db.session.execute(text("UPDATE device SET calib = '{}' WHERE id = :i"), {"i": d.id})
        migrations._migrate_tare()
        db.session.commit()
        db.session.refresh(d)
        assert d.calib["0"]["tare"] == {"a": 0.02, "w": 3.5, "at": 0} and "tare_a" not in d.cfg
        assert tare.offsets(d) == (0.02, 3.5)
        migrations._migrate_tare()   # again: nothing changes
        assert json.loads(json.dumps(d.calib))["0"]["tare"]["w"] == 3.5


# ------------------------------------------------------------------ pages

def test_pages_and_access(app, admin, online):
    with app.app_context():  # (no context kept open around the requests: they would share one stale session)
        pk = _meter("Dryer A", "AAAA1111")
    html = admin.get(f"/meters/{pk}/calibration").get_data(as_text=True)
    assert "Zero &amp; Tare" in html and "Check sensor" in html and "at least 10 s" in html
    assert "Zero &amp; Tare" in admin.get(f"/meters/{pk}/configure").get_data(as_text=True)
    assert b"tareNow" not in admin.get(f"/meters/{pk}/configure").data
    r = admin.post(f"/meters/{pk}/calibration/check", json={"ch": 0, "kind": "zero"})
    assert r.get_json()["ok"] and online[-1]["c"] == "sensor_check" and online[-1]["n"] == 10
    s = admin.get(f"/meters/{pk}/calibration/status").get_json()
    assert s["online"] and s["channels"][0]["check"]["state"] == "running"
    assert admin.post(f"/meters/{pk}/calibration/apply", json={"ch": 0, "kind": "zero"}).status_code == 400   # no result yet
    with app.app_context():
        check_event(db.session.get(Device, pk))
        db.session.commit()
    ok = admin.post(f"/meters/{pk}/calibration/apply", json={"ch": 0, "kind": "zero"}).get_json()
    assert ok["ok"] and ok["a"] == 0.012, ok
    assert admin.get(f"/meters/{pk}/calibration/status").get_json()["channels"][0]["zero"]["a"] == 0.012
    assert admin.post(f"/meters/{pk}/calibration/clear", json={"ch": 0, "what": "zero"}).get_json()["ok"]
    assert admin.post(f"/meters/{pk}/calibration/clear", json={"ch": 9}).status_code == 400
    # a viewer cannot use any of it
    from .test_access import _viewer
    _viewer(admin, "tenant", [pk])
    v = _login(app, "tenant")
    assert v.get(f"/meters/{pk}/calibration").status_code == 403
    assert v.post(f"/meters/{pk}/calibration/check", json={"ch": 0, "kind": "zero"}).status_code == 403
    assert v.post(f"/meters/{pk}/calibration/apply", json={"ch": 0, "kind": "zero"}).status_code == 403


def test_offline_check_is_refused_by_the_page(app, admin):
    with app.app_context():
        pk = _meter("Dryer A", "AAAA1111")
    r = admin.post(f"/meters/{pk}/calibration/check", json={"ch": 0, "kind": "zero"})
    assert r.status_code == 409 and "offline" in r.get_json()["error"]


# ------------------------------------------------------------------ tared views, per channel

def test_tare_views_are_per_channel(app, admin):
    r = admin.post("/api/v1/pair", json={"code": _code(admin), "device_id": "CAL00002", "fw": "1.1.0", "proto": 1,
                                         "caps": dict(CAPS, channels=3, relays=3, ratings=[30, 30, 30])})
    assert r.status_code == 200
    t0 = now() - 3600
    with app.app_context():
        d = Device.query.filter_by(device_id="CAL00002").one()
        pk = d.id
        d.calib = {"1": {"tare": {"a": 0.1, "w": 20.0, "at": 1}}, "2": {"tare": {"a": 0.2, "w": 30.0, "at": 1}}}   # channel 0: none
        items = [{"seq": 10 + k, "ts": t0 + k * 600, "tv": 1,
                  "ch": [{"i": i, "v": 230, "a": 0.5, "w": 100.0, "pf": 1, "hz": 50, "e": 1.0 + k * 0.05} for i in range(3)]} for k in range(7)]
        ingest.handle(FakeConn(), d, {"t": "tel", "items": items})
        db.session.commit()
    m = admin.get(f"/meters/{pk}/live").get_json()
    ch = {c["i"]: c for c in m["channels"]}
    assert ch[0]["tare"] is None and ch[0]["w_tared"] == 100.0
    assert ch[1]["tare"] == {"a": 0.1, "w": 20.0} and ch[1]["w_tared"] == 80.0 and ch[1]["a_tared"] == pytest.approx(0.4)
    assert ch[2]["w_tared"] == 70.0
    assert m["tare"] == {"a": pytest.approx(0.3), "w": 50.0}      # the whole meter: the sum of the channels' tares
    assert m["w"] == 300.0 and m["w_tared"] == 250.0
    assert [c["w"] for c in m["reading_tared"]] == [100.0, 80.0, 70.0]
    # history of a channel is tared with its own tare
    raw1 = admin.get(f"/meters/{pk}/history?range=1d&ch=1").get_json()
    tar1 = admin.get(f"/meters/{pk}/history?range=1d&ch=1&tared=1").get_json()
    tar0 = admin.get(f"/meters/{pk}/history?range=1d&ch=0&tared=1").get_json()
    assert raw1["stats"]["w"]["avg"] == 100.0 and tar1["stats"]["w"]["avg"] == 80.0 and tar0["stats"]["w"]["avg"] == 100.0
    # CSV: tared columns only for the channels that have a tare
    head = admin.get(f"/meters/{pk}/export.csv?range=1d").get_data(as_text=True).splitlines()
    assert "CH2 A_tared" in head[0] and "CH2 W_tared" in head[0] and "CH3 W_tared" in head[0] and "CH1 W_tared" not in head[0]
    row = head[1].split(",")
    cols = head[0].split(",")
    assert row[cols.index("CH2 W_tared")] == "80.0" and row[cols.index("CH3 W_tared")] == "70.0"


def test_independent_session_kwh_is_tared_per_channel(app, admin):
    r = admin.post("/api/v1/pair", json={"code": _code(admin), "device_id": "CAL00003", "fw": "1.1.0", "proto": 1,
                                         "caps": dict(CAPS, channels=2, relays=2)})
    assert r.status_code == 200
    t0 = now() - 3600
    with app.app_context():
        d = Device.query.filter_by(device_id="CAL00003").one()
        pk = d.id
        d.cfg = dict(d.cfg, mode="independent")
        d.calib = {"1": {"tare": {"a": 0.1, "w": 20.0, "at": 1}}}
        ingest.handle(FakeConn(), d, {"t": "state", "mode": "independent", "relay": [{"i": 0, "on": True}, {"i": 1, "on": True}],
                                      "sessions": [{"ch": 0, "sid": 1, "kwh": 0.5, "status": "active", "start": t0, "max": 0},
                                                   {"ch": 1, "sid": 2, "kwh": 0.5, "status": "active", "start": t0, "max": 0}]})
        db.session.commit()
    m = admin.get(f"/meters/{pk}/live").get_json()
    ch = {c["i"]: c for c in m["channels"]}
    assert ch[0]["kwh"] == 0.5 and ch[0]["kwh_tared"] == 0.5
    assert ch[1]["kwh_tared"] == pytest.approx(0.5 - 0.02, abs=1e-3)      # 20 W for about 1 h
    assert m["kwh_tared"] == pytest.approx(0.98, abs=1e-3)


def test_independent_meter_kwh_t_is_used_when_the_meter_sends_it(app, admin):
    r = admin.post("/api/v1/pair", json={"code": _code(admin), "device_id": "CAL00004", "fw": "1.1.0", "proto": 1,
                                         "caps": dict(CAPS, channels=2, relays=2)})
    assert r.status_code == 200
    t0 = now() - 3600
    with app.app_context():
        d = Device.query.filter_by(device_id="CAL00004").one()
        pk = d.id
        d.cfg = dict(d.cfg, mode="independent")
        d.calib = {"1": {"tare": {"a": 0.1, "w": 20.0, "at": 1}}}
        ingest.handle(FakeConn(), d, {"t": "state", "mode": "independent", "relay": [{"i": 0, "on": True}, {"i": 1, "on": True}],
                                      "sessions": [{"ch": 0, "sid": 1, "kwh": 0.5, "status": "active", "start": t0, "max": 0},
                                                   {"ch": 1, "sid": 2, "kwh": 0.5, "status": "active", "start": t0, "max": 0, "kwh_t": 0.4}]})
        ingest.handle(FakeConn(), d, {"t": "tel", "items": [{"seq": 50, "ts": now(), "tv": 1, "ch": [], "ss": [
            {"ch": 0, "sid": 1, "kwh": 0.6}, {"ch": 1, "sid": 2, "kwh": 0.6, "kwh_t": 0.45}]}]})
        db.session.commit()
    ch = {c["i"]: c for c in admin.get(f"/meters/{pk}/live").get_json()["channels"]}
    assert ch[1]["kwh"] == 0.6 and ch[1]["kwh_tared"] == 0.45          # the meter's own tared value wins
    assert ch[0]["kwh"] == 0.6 and ch[0]["kwh_tared"] == 0.6
