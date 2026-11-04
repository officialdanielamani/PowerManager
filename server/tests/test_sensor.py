"""Sensor timeout: config, events / incident / notification, and the latest reading on errors."""
import pytest

from app.extensions import db
from app.models import Device, Incident, Notification, NotificationRule, User, now
from app.routes.device_api import ensure_device_defaults
from app.services import devconfig, ingest, settings

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


def test_sensor_timeout_config(app, device):
    base = {"report_s": "60", "sess_mode": "none", "daily_at": "00:00"}
    cfg, errors = devconfig.parse_form(base, device)
    assert not errors and devconfig.to_device(cfg, 1)["cfg"]["sensor"] == {"timeout_s": 10, "action": "hold"}
    cfg, errors = devconfig.parse_form({**base, "sensor_to_s": "30", "sensor_action": "end"}, device)
    assert not errors and devconfig.to_device(cfg, 1)["cfg"]["sensor"] == {"timeout_s": 30, "action": "end"}
    cfg, errors = devconfig.parse_form({**base, "sensor_to_s": "0"}, device)
    assert not errors and cfg["sensor_to_s"] == 0  # off
    _, errors = devconfig.parse_form({**base, "sensor_to_s": "2"}, device)
    assert errors


def test_sensor_error_incident_and_notification(app, device):
    u = User(username="adm", role="admin")
    u.set_password("Str0ng!Passw0rd")
    db.session.add(u)
    db.session.flush()
    db.session.add(NotificationRule(device_fk=device.id, user_fk=u.id, type="sensor_error", enabled=True))
    db.session.commit()
    conn = FakeConn()
    t = now() - 100
    ingest.handle(conn, device, {"t": "evt", "seq": 1, "ts": t, "tv": 1, "e": "sensor_error", "kind": "bad",
                                 "after_s": 10, "action": "hold"})
    assert Incident.query.filter_by(type="sensor", ended_at=None).count() == 1
    ingest.handle(conn, device, {"t": "evt", "seq": 2, "ts": t + 25, "tv": 1, "e": "sensor_ok"})
    inc = Incident.query.filter_by(type="sensor").one()
    assert inc.duration_s == 25 and inc.detail["kind"] == "bad"
    n = Notification.query.filter_by(type="sensor_error").one()
    assert "10 s" in n.message and "relay off" in n.message


def test_latest_reading_shows_sensor_error(app, device):
    from app.routes.main import meter_summary
    conn = FakeConn()
    t = now() - 50
    ingest.handle(conn, device, {"t": "tel", "items": [
        {"seq": 1, "ts": t, "tv": 1, "ch": [{"i": 0, "v": 230, "a": 1, "w": 230, "pf": 1, "hz": 50, "e": 1.0}]}]})
    ingest.handle(conn, device, {"t": "tel", "items": [{"seq": 2, "ts": t + 20, "tv": 1, "ch": [], "se": "no_reply"}]})
    with app.test_request_context():
        m = meter_summary(device, show_price=False)
    assert m["reading"] == [] and m["w"] is None and m["reading_err"] == "no_reply"
    assert m["reading_ts"] == t + 20 and m["reading_ok_at"] == settings.fmt_ts(t)
    # Readings come back: the error goes away
    ingest.handle(conn, device, {"t": "tel", "items": [
        {"seq": 3, "ts": t + 30, "tv": 1, "ch": [{"i": 0, "v": 231, "a": 1, "w": 231, "pf": 1, "hz": 50, "e": 1.1}]}]})
    with app.test_request_context():
        m = meter_summary(device, show_price=False)
    assert m["reading_err"] is None and m["w"] == 231


def test_state_clears_sensor_flag(app, device):
    conn = FakeConn()
    ingest.handle(conn, device, {"t": "state", "relay": [{"i": 0, "on": False, "block": "sensor"}], "sensor_err": "no_reply"})
    assert device.last_state["sensor_err"] == "no_reply"
    ingest.handle(conn, device, {"t": "state", "relay": [{"i": 0, "on": True, "block": None}]})
    assert "sensor_err" not in device.last_state


def test_hello_state_opens_and_closes_sensor_incident(app, device):
    conn = FakeConn()
    st = {"relay": [{"i": 0, "on": False, "block": "sensor"}], "sensor_err": "no_reply"}
    ingest.handle(conn, device, {"t": "hello", "up": 100, "boot": 1, "cfg_ver": 0, "state": st})
    assert Incident.query.filter_by(type="sensor", ended_at=None).count() == 1
    ingest.handle(conn, device, {"t": "hello", "up": 200, "boot": 1, "cfg_ver": 0,
                                 "state": {"relay": [{"i": 0, "on": True, "block": None}]}})
    assert Incident.query.filter_by(type="sensor", ended_at=None).count() == 0
