import pytest

from app.extensions import db
from app.models import Device, Incident, Measurement, now
from app.routes.device_api import ensure_device_defaults
from app.services import commands, devconfig, history, incidents, ingest, rollup, settings

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
    assert [m["t"] for m in conn.sent if m["t"] != "link_key"] == ["welcome", "cfg"]
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


def test_session_progress_from_telemetry(app, device):
    """Session kWh comes with every reading, not only with (rare) state messages."""
    conn = FakeConn()
    t = now() - 30
    ingest.handle(conn, device, {"t": "evt", "seq": 1, "ts": t, "tv": 1, "e": "session_start", "sid": 4, "max": 2.0})
    ingest.handle(conn, device, {"t": "state", "sess": {"sid": 4, "kwh": 0.1, "max": 2.0, "status": "active"}})
    ingest.handle(conn, device, {"t": "tel", "items": [{**tel(2, t + 10, 5.0), "s": {"sid": 4, "kwh": 0.75}}]})
    assert device.last_state["sess"]["kwh"] == 0.75
    from app.models import Session
    assert Session.query.filter_by(sid=4).one().kwh_used == 0.75
    # telemetry from an older session is ignored
    ingest.handle(conn, device, {"t": "tel", "items": [{**tel(3, t + 11, 5.1), "s": {"sid": 3, "kwh": 9.9}}]})
    assert device.last_state["sess"]["kwh"] == 0.75


def test_kwh_action_option_reaches_device(app, device):
    with app.app_context():
        form = {"report_s": "60", "sess_mode": "duration", "dur_m": "5", "kwh_max_0": "2", "amp_max_0": "0",
                "daily_at": "00:00"}
        for action, legacy_end in (("end", True), ("hold", True), ("warn", False)):  # warn = "run as usual"
            cfg, errors = devconfig.parse_form({**form, "kwh_action_0": action}, device)
            ch = devconfig.to_device(cfg, 1)["cfg"]["ch"][0]
            assert not errors and ch["kwh_action"] == action and ch["kwh_end"] is legacy_end
        cfg, _ = devconfig.parse_form(form, device)  # nothing chosen -> default "hold"
        assert devconfig.to_device(cfg, 1)["cfg"]["ch"][0]["kwh_action"] == "hold"
        # config saved by v0.2 (kwh_end only)
        assert devconfig.to_device({"ch": [{"kwh_max": 1, "kwh_end": False}]}, 1)["cfg"]["ch"][0]["kwh_action"] == "warn"


def _start(conn, device, sid, ts):
    ingest.handle(conn, device, {"t": "evt", "seq": sid * 10, "ts": ts, "tv": 1, "e": "session_start", "sid": sid, "max": 1.0})
    ingest.handle(conn, device, {"t": "state", "sess": {"sid": sid, "kwh": 0, "max": 1.0, "status": "active"}})


def test_server_ends_session_at_kwh_limit_even_without_session_end(app, device):
    """Older firmware only sent session_end on Renew; the session must still end at the limit."""
    from app.models import Session
    conn = FakeConn()
    t = now() - 600
    _start(conn, device, 1, t)
    ingest.handle(conn, device, {"t": "evt", "seq": 11, "ts": t + 120, "tv": 1, "e": "quota", "ch": 0, "kwh": 1.0})
    s = Session.query.filter_by(sid=1).one()
    assert s.ended_at == t + 120 and s.end_reason == "quota"
    # later Renew from old firmware sends session_end "manual": the kWh end is kept
    ingest.handle(conn, device, {"t": "evt", "seq": 12, "ts": t + 500, "tv": 1, "e": "session_end", "sid": 1, "reason": "manual"})
    s = Session.query.filter_by(sid=1).one()
    assert s.ended_at == t + 120 and s.end_reason == "quota"


def test_hold_action_keeps_session_open(app, device):
    from app.models import Session
    conn = FakeConn()
    t = now() - 600
    _start(conn, device, 2, t)
    ingest.handle(conn, device, {"t": "evt", "seq": 21, "ts": t + 60, "tv": 1, "e": "quota", "kwh": 1.0, "action": "hold"})
    assert Session.query.filter_by(sid=2).one().ended_at is None
    ingest.handle(conn, device, {"t": "evt", "seq": 22, "ts": t + 300, "tv": 1, "e": "session_end", "sid": 2, "reason": "expired"})
    assert Session.query.filter_by(sid=2).one().ended_at == t + 300


def test_session_price_locked_at_start(app, device):
    from app.models import Session
    device.currency, device.price_per_kwh = "MYR", 0.21
    conn = FakeConn()
    _start(conn, device, 3, now() - 100)
    ingest.handle(conn, device, {"t": "tel", "items": [{**tel(31, now() - 50, 5.0), "s": {"sid": 3, "kwh": 10.0}}]})
    device.price_per_kwh = 0.50  # price change does not touch a running session
    s = Session.query.filter_by(sid=3).one()
    assert s.price_per_kwh == 0.21 and s.currency == "MYR" and s.cost() == 2.1


def test_voltage_and_tamper_config(app, device):
    with app.app_context():
        base = {"report_s": "60", "sess_mode": "none", "daily_at": "00:00"}
        cfg, errors = devconfig.parse_form({**base, "volt_min": "200", "volt_max": "250", "volt_action": "end",
                                            "tamper_protect": "on", "tamper_action": "run"}, device)
        doc = devconfig.to_device(cfg, 1)["cfg"]
        assert not errors and doc["volt"] == {"min": 200.0, "max": 250.0, "action": "end"}
        assert doc["tamper"] == {"protect": True, "action": "run"}
        cfg, errors = devconfig.parse_form({**base, "volt_min": "250", "volt_max": "200"}, device)
        assert "Operating voltage: Min must be lower than Max" in errors
        cfg, _ = devconfig.parse_form(base, device)  # defaults
        doc = devconfig.to_device(cfg, 1)["cfg"]
        assert doc["volt"]["action"] == "hold" and doc["tamper"] == {"protect": False, "action": "hold"}


def test_voltage_incident_and_notification(app, device):
    from app.models import Incident, Notification, NotificationRule, User
    u = User(username="adm", role="admin")
    u.set_password("Str0ng!Passw0rd")
    db.session.add(u)
    db.session.flush()
    db.session.add(NotificationRule(device_fk=device.id, user_fk=u.id, type="voltage_range", enabled=True))
    db.session.commit()
    conn = FakeConn()
    t = now() - 100
    ingest.handle(conn, device, {"t": "evt", "seq": 1, "ts": t, "tv": 1, "e": "voltage_out", "v": 262.1,
                                 "min": 200, "max": 250, "dir": "high", "action": "hold"})
    ingest.handle(conn, device, {"t": "evt", "seq": 2, "ts": t + 40, "tv": 1, "e": "voltage_ok", "v": 231.0})
    inc = Incident.query.filter_by(type="voltage").one()
    assert inc.duration_s == 40 and inc.detail["dir"] == "high"
    n = Notification.query.filter_by(type="voltage_range").one()
    assert "too high" in n.message and "relay off" in n.message


def test_session_ended_by_protection(app, device):
    from app.routes.main import meter_summary
    conn = FakeConn()
    ingest.handle(conn, device, {"t": "state", "sess": {"sid": 5, "kwh": 0.2, "max": 0, "status": "ended",
                                                        "end_reason": "tamper", "ended": True},
                                 "relay": [{"i": 0, "on": False, "block": "ended"}]})
    with app.test_request_context():
        m = meter_summary(device, show_price=False)
    assert m["status"] == "ended_tamper" and m["sess_ended"] and m["relay_block"] == "ended"
    assert commands.relay_block_reason(device) == "session_expired"


def test_float_limit_from_meter_is_rounded(app, device):
    """Meters store limits as 32-bit floats: 0.05 arrives as 0.050000001."""
    from app.models import Session
    from app.routes.main import meter_summary
    conn = FakeConn()
    ingest.handle(conn, device, {"t": "evt", "seq": 1, "ts": now() - 10, "tv": 1, "e": "session_start",
                                 "sid": 2, "max": 0.050000001})
    ingest.handle(conn, device, {"t": "state", "sess": {"sid": 2, "kwh": 0.01, "max": 0.050000001, "status": "active"},
                                 "relay": [{"i": 0, "on": True, "block": None}]})
    assert Session.query.filter_by(sid=2).one().kwh_max == 0.05
    with app.test_request_context():
        assert meter_summary(device, show_price=False)["kwh_max"] == 0.05
