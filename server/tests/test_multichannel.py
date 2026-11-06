"""Multi-channel AC meter (rbAmp firmware): config, sessions per mode, per-channel commands and display."""
import pytest

from app.extensions import db
from app.models import Command, Device, Incident, Notification, NotificationRule, Session, User, now
from app.services import devconfig, ingest

from .conftest import FakeConn
from .test_pairing import _code

CAPS = {"kind": "ac", "mc": True, "sensor": "rbamp", "variant": "UI2", "channels": 2, "relays": 2,
        "voltage": True, "drdy": True, "amp_rating": 34.8, "ratings": [30, 30]}


def _pair_mc(client, code, device_id="MCM00001"):
    return client.post("/api/v1/pair", json={"code": code, "device_id": device_id, "fw": "0.7.0", "proto": 1, "caps": CAPS})


@pytest.fixture()
def mc(app, admin):
    r = _pair_mc(admin, _code(admin))
    assert r.status_code == 200
    cfg = r.get_json()["config"]["cfg"]
    assert cfg["mode"] == "shared" and len(cfg["ch"]) == 2  # Shared is the default
    with app.app_context():
        yield Device.query.filter_by(device_id="MCM00001").one().id


def _form(**kw):
    base = {"name": "Panel", "report_s": "60", "read_s": "60", "sess_mode": "none", "daily_at": "00:00"}
    base.update(kw)
    return base


def test_config_form_independent(app, admin, mc):
    r = admin.post(f"/meters/{mc}/configure", data=_form(
        mode="independent", drdy="on", read_s="1", name_0="Kitchen", name_1="Tenant B", amp_max_0="16", amp_max_1="20",
        kwh_max_0="8", kwh_action_0="end", kwh_max_1="12", sess_mode_1="daily", daily_at_1="06:00", auto_1="on"))
    assert r.status_code in (200, 302)
    with app.app_context():
        d = db.session.get(Device, mc)
        assert d.mc_mode == "independent" and d.channel_name(1) == "CH2 Tenant B"
        doc = devconfig.to_device(d.cfg, 1)["cfg"]
    assert doc["mode"] == "independent" and doc["drdy"] is True and doc["read_s"] == 1
    assert doc["ch"][0]["kwh_max"] == 8 and doc["ch"][0]["kwh_action"] == "end" and doc["ch"][0]["amp_max"] == 16
    assert doc["ch"][1]["sess"]["mode"] == "daily" and doc["ch"][1]["sess"]["auto"] is True
    assert admin.get(f"/meters/{mc}/configure").status_code == 200


def test_read_interval_limits(app, admin, mc):
    with app.app_context():
        d = db.session.get(Device, mc)
        _, errors = devconfig.parse_form(_form(read_s="1"), d)
        assert not errors  # Normal mode: 1 s for every meter
        _, errors = devconfig.parse_form(_form(read_s="0.5"), d)
        assert errors
        cfg, errors = devconfig.parse_form(_form(drdy="on", report_s_drdy="15"), d)
        assert not errors  # DRDY mode: no read interval, send 1-90 s
        doc = devconfig.to_device(cfg, 1)["cfg"]
        assert doc["drdy"] is True and doc["report_s"] == 15
        _, errors = devconfig.parse_form(_form(drdy="on", report_s_drdy="91"), d)
        assert any("Send every" in e for e in errors)
        cfg, errors = devconfig.parse_form(_form(drdy="off", read_s="5", report_s="60"), d)  # Normal mode again
        assert not errors and cfg["drdy"] is False and cfg["report_s"] == 60


def test_drdy_frames_and_wide_csv(app, admin, mc):
    from app.models import Measurement
    with app.app_context():
        d = db.session.get(Device, mc)
        t = now() - 30
        frames = [[k * 100, 230.0 + k, 1.0, 230.0, 0.99, 2.0, 460.0 + k, 0.98] for k in range(12)]  # 12 windows, 1.2 s
        ingest.handle(FakeConn(), d, {"t": "tel", "items": [{"seq": 50, "ts": t, "tv": 1, "hz": 50, "e": [1.5, 3.25],
                                                              "fr": frames, "s": {"sid": 0, "kwh": 0}}]})
        rows = Measurement.query.filter_by(device_fk=d.id).order_by(Measurement.ts, Measurement.ms, Measurement.ch).all()
        assert len(rows) == 24  # 12 windows x 2 channels
        assert (rows[-1].ts, rows[-1].ms) == (t + 1, 100) and rows[-1].w == 471.0 and rows[-1].e == 3.25
        assert d.last_reading["ch"][1]["w"] == 471.0  # latest data = the last window
        d.cfg = dict(d.cfg, ch=[dict(d.cfg["ch"][0], name="Kitchen"), d.cfg["ch"][1]])
        db.session.commit()
    csv = admin.get(f"/meters/{mc}/export.csv?range=1d").get_data(as_text=True).splitlines()
    header = csv[0].split(",")
    assert "CH1 Kitchen A" in header and "CH2 W" in header and "channel" not in header
    assert len(csv) == 1 + 12  # one row per window, both channels side by side
    assert ",1.000,230.0,0.99," in csv[1]  # same decimals as on screen (A 3, W 1, PF 2)


def test_negative_power_is_kept(app, admin, mc):
    from app.models import Measurement
    with app.app_context():
        d = db.session.get(Device, mc)
        ingest.handle(FakeConn(), d, {"t": "tel", "items": [{"seq": 70, "ts": now() - 5, "tv": 1,
                                      "ch": [{"i": 0, "v": 230, "a": 0.5, "w": -110.5, "pf": -0.96}]}]})
        m = Measurement.query.filter_by(device_fk=d.id, seq=70).one()
        assert m.w == -110.5 and m.pf == -0.96  # clamp reversed: visible, not dropped


def test_independent_sessions_quota_and_display(app, admin, mc):
    from app.routes.main import meter_summary
    with app.app_context():
        d = db.session.get(Device, mc)
        d.cfg = dict(d.cfg, mode="independent")
        u = User.query.filter_by(username="admin").one()
        db.session.add(NotificationRule(device_fk=d.id, user_fk=u.id, type="kwh_exceeded", enabled=True))
        db.session.commit()
        conn, t = FakeConn(), now() - 100
        ingest.handle(conn, d, {"t": "evt", "seq": 1, "ts": t, "tv": 1, "e": "session_start", "sid": 3, "ch": 0, "max": 8})
        ingest.handle(conn, d, {"t": "evt", "seq": 2, "ts": t, "tv": 1, "e": "session_start", "sid": 4, "ch": 1, "max": 12})
        ingest.handle(conn, d, {"t": "state", "mode": "independent", "tripped": False, "tamper": False, "ac": True,
                                "relay": [{"i": 0, "on": True, "want": True, "block": None, "tripped": False},
                                          {"i": 1, "on": False, "want": True, "block": "quota", "tripped": False}],
                                "sessions": [{"ch": 0, "sid": 3, "kwh": 1.5, "max": 8, "status": "active", "ended": False},
                                             {"ch": 1, "sid": 4, "kwh": 12.01, "max": 12, "status": "quota", "ended": True}]})
        ingest.handle(conn, d, {"t": "evt", "seq": 3, "ts": t + 50, "tv": 1, "e": "quota", "ch": 1, "kwh": 12.01,
                                "action": "end", "end": True})
        ingest.handle(conn, d, {"t": "tel", "items": [{"seq": 4, "ts": t + 60, "tv": 1,
                               "ch": [{"i": 0, "v": 231, "a": 2.1, "w": 480, "pf": 0.99, "hz": 50, "e": 3.2},
                                      {"i": 1, "v": 231, "a": 0, "w": 0, "pf": 0, "hz": 50, "e": 15.0}],
                               "ss": [{"ch": 0, "sid": 3, "kwh": 1.75}, {"ch": 1, "sid": 4, "kwh": 12.01}]}]})
        s3 = Session.query.filter_by(sid=3).one()
        s4 = Session.query.filter_by(sid=4).one()
        assert (s3.ch, s4.ch) == (0, 1) and s4.ended_at == t + 50 and s4.end_reason == "quota"
        assert s3.kwh_used == 1.75
        n = Notification.query.filter_by(type="kwh_exceeded").one()
        assert "CH2" in n.message
        with app.test_request_context():
            m = meter_summary(d, show_price=False)
        assert m["mc"] and m["mode"] == "independent" and len(m["channels"]) == 2
        c0, c1 = m["channels"]
        assert c0["relay_on"] and c0["w"] == 480 and c0["status"] == "active" and c0["kwh"] == 1.75
        assert c1["status"] == "quota" and not c1["relay_on"]
        assert m["kwh"] == 13.76 and m["kwh_max"] == 20


def test_channel_commands(app, admin, mc):
    with app.app_context():
        d = db.session.get(Device, mc)
        d.last_state = {"mode": "shared", "relay": [{"i": 0, "tripped": False}, {"i": 1, "tripped": True}],
                        "sess": {"sid": 1, "status": "active"}}
        db.session.commit()
    ok = admin.post(f"/meters/{mc}/action", json={"action": "relay_on", "ch": 0}).get_json()
    assert ok["ok"]
    blocked = admin.post(f"/meters/{mc}/action", json={"action": "relay_on", "ch": 1}).get_json()
    assert not blocked["ok"] and blocked["error"] == "tripped"
    assert admin.post(f"/meters/{mc}/action", json={"action": "end"}).get_json()["ok"]
    assert admin.post(f"/meters/{mc}/action", json={"action": "renew", "ch": 1}).get_json()["ok"]
    with app.app_context():
        cmds = [(c.c, c.params) for c in Command.query.order_by(Command.id)]
    assert ("relay", {"on": True, "ch": 0}) in cmds and ("end", {}) in cmds and ("renew", {"ch": 1}) in cmds


def test_trip_incidents_per_channel(app, admin, mc):
    with app.app_context():
        d = db.session.get(Device, mc)
        conn, t = FakeConn(), now() - 60
        ingest.handle(conn, d, {"t": "evt", "seq": 1, "ts": t, "tv": 1, "e": "overcurrent", "ch": 1, "val": 21.0, "limit": 20})
        ingest.handle(conn, d, {"t": "evt", "seq": 2, "ts": t + 10, "tv": 1, "e": "overcurrent", "ch": 0, "val": 17.0, "limit": 16})
        ingest.handle(conn, d, {"t": "evt", "seq": 3, "ts": t + 30, "tv": 1, "e": "trip_reset", "ch": 1, "src": "server"})
        assert Incident.query.filter_by(type="trip", ch=1).one().duration_s == 30
        assert Incident.query.filter_by(type="trip", ch=0, ended_at=None).count() == 1


def test_pages_render_and_bulk_skips_mc(app, admin, mc):
    html = admin.get("/").get_data(as_text=True)
    assert 'data-field="mc-mini"' in html
    html = admin.get(f"/meters/{mc}").get_data(as_text=True)
    assert 'id="mcChannels"' in html and "Renew Now" in html and "End session" in html
    r = admin.get(f"/meters/bulk/configure?ids={mc}", follow_redirects=True).get_data(as_text=True)
    assert "Skipped multi-channel" in r


def test_telegram_lists_channels(app, admin, mc):
    from app.services import telegram_bot as bot
    with app.app_context(), app.test_request_context():
        d = db.session.get(Device, mc)
        d.cfg = dict(d.cfg, ch=[dict(d.cfg["ch"][0], name="Kitchen"), d.cfg["ch"][1]])
        d.last_reading = {"ts": now(), "ch": [{"i": 0, "v": 230, "a": 1.0, "w": 230}, {"i": 1, "v": 230, "a": 0.5, "w": 115}]}
        d.last_state = {"relay": [{"i": 0, "on": True}, {"i": 1, "on": False}], "sess": {"sid": 1, "kwh": 0.3}}
        txt = bot.reading_text(d, User.query.filter_by(username="admin").one())
    assert "CH1 Kitchen: 230.0 W" in txt and "CH2: 115.0 W" in txt and "OFF" in txt


def test_subsecond_readings_keep_ms(app, admin, mc):
    from app.models import Measurement
    from app.services import history
    with app.app_context():
        d = db.session.get(Device, mc)
        t = now() - 30
        items = [{"seq": 10 + k, "ts": t, "ms": k * 200, "tv": 1, "ch": [{"i": 0, "v": 230, "a": 1, "w": 230 + k}]}
                 for k in range(5)]
        ingest.handle(FakeConn(), d, {"t": "tel", "items": items})
        rows = Measurement.query.filter_by(device_fk=d.id).order_by(Measurement.seq).all()
        assert [r.ms for r in rows] == [0, 200, 400, 600, 800]
        _, header, out = history.csv_rows(d.id, 0, t - 5, t + 5)
        assert header[-1] == "ms" and [r[1][-1] for r in out] == [0, 200, 400, 600, 800]


def test_aux_relays(app, admin):
    from app.routes.main import meter_summary
    caps = dict(CAPS, aux=2)  # 2 channels on a 4-relay board: relays 3 and 4 are AUX relays
    assert _pair_mc(admin, _code(admin), "MCAUX001").status_code == 200
    with app.app_context():
        d = Device.query.filter_by(device_id="MCAUX001").one()
        d.caps = caps
        assert devconfig.fit_channels(d) and len(d.cfg["aux"]) == 2
        db.session.commit()
        pk = d.id
    r = admin.post(f"/meters/{pk}/configure", data=_form(aux_name_0="Pump", aux_name_1="Fan"))
    assert r.status_code in (200, 302)
    assert admin.post(f"/meters/{pk}/action", json={"action": "aux_on", "i": 1}).get_json()["ok"]
    assert admin.post(f"/meters/{pk}/action", json={"action": "aux_on", "i": 2}).status_code == 400  # no AUX3
    with app.app_context():
        d = db.session.get(Device, pk)
        assert devconfig.to_device(d.cfg, 1)["cfg"]["aux"] == [{"i": 0, "name": "Pump"}, {"i": 1, "name": "Fan"}]
        assert ("aux", {"i": 1, "on": True}) in [(c.c, c.params) for c in Command.query.filter_by(device_fk=pk)]
        ingest.handle(FakeConn(), d, {"t": "state", "mode": "shared", "relay": [{"i": 0, "on": False, "tripped": False}],
                                      "sess": {"sid": 0, "status": "active"},
                                      "aux": [{"i": 0, "relay": 3, "on": False}, {"i": 1, "relay": 4, "on": True}]})
        with app.test_request_context():
            m = meter_summary(d, show_price=False)
        assert m["aux"][1] == {"i": 1, "relay": 4, "on": True, "name": "AUX2 Fan"}
        assert m["status"] == "none"  # paired, no session until Renew


def test_no_session_blocks_relay_on(app, admin, mc):
    with app.app_context():
        d = db.session.get(Device, mc)
        d.last_state = {"mode": "shared", "relay": [{"i": 0, "tripped": False}], "sess": {"sid": 0, "status": "active"}}
        db.session.commit()
    r = admin.post(f"/meters/{mc}/action", json={"action": "relay_on", "ch": 0}).get_json()
    assert not r["ok"] and r["error"] == "no_session" and "Renew" in r["message"]
