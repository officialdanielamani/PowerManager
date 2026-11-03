from app.extensions import db
from app.models import Device, Notification, NotificationRule, User
from app.routes.device_api import ensure_device_defaults
from app.services import ingest, settings

from .conftest import ADMIN_PW, FakeConn

VPW = "Vi3wer!Passw0rd"


def _meter(name, device_id):
    d = Device(device_id=device_id, name=name, caps={"channels": 1}, cfg={}, pair_gen=1)
    db.session.add(d)
    db.session.flush()
    ensure_device_defaults(d, reset=True)
    db.session.commit()
    return d.id


def _viewer(admin, name, meters=(), view_all=False):
    data = {"what": "add", "username": name, "password": VPW, "confirm": VPW,
            "meters": [str(m) for m in meters]}
    if view_all:
        data["view_all"] = "on"
    assert admin.post("/settings/users", data=data).status_code == 302


def _login(app, name, pw=VPW):
    c = app.test_client()
    c.post("/login", data={"username": name, "password": pw})
    return c


def test_viewer_sees_only_linked_meters(app, admin):
    with app.app_context():
        a, b = _meter("Dryer A", "AAAA1111"), _meter("Washer B", "BBBB2222")
    _viewer(admin, "tenant1", [a])
    _viewer(admin, "boss2", view_all=True)
    t = _login(app, "tenant1")
    names = [m["name"] for m in t.get("/api/ui/meters").get_json()["meters"]]
    assert names == ["Dryer A"]
    assert t.get(f"/meters/{a}").status_code == 200
    for path in (f"/meters/{b}", f"/meters/{b}/live", f"/meters/{b}/history", f"/meters/{b}/export.csv",
                 f"/meters/{b}/notifications"):
        assert t.get(path).status_code == 404, path
    assert t.get(f"/meters/{a}/configure").status_code == 403
    boss = _login(app, "boss2")
    assert len(boss.get("/api/ui/meters").get_json()["meters"]) == 2


def test_link_from_meter_configuration(app, admin):
    with app.app_context():
        a = _meter("Dryer A", "AAAA1111")
    _viewer(admin, "tenant1")
    with app.app_context():
        uid = User.query.filter_by(username="tenant1").one().id
    form = {"name": "Dryer A", "info": "", "report_s": "60", "sess_mode": "none", "kwh_max_0": "0",
            "amp_max_0": "0", "daily_at": "00:00", "viewers": [str(uid)]}
    admin.post(f"/meters/{a}/configure", data=form)
    t = _login(app, "tenant1")
    assert t.get(f"/meters/{a}").status_code == 200


def test_notifications_are_per_user(app, admin):
    with app.app_context():
        a = _meter("Dryer A", "AAAA1111")
    _viewer(admin, "tenant1", [a])
    # admin: only panel open + no voltage; viewer: everything
    admin.post(f"/meters/{a}/notifications", data={"notify_panel_open": "on", "notify_no_voltage": "on",
                                                    "notify_disconnected_min": "5"})
    t = _login(app, "tenant1")
    all_on = {f"notify_{k}": "on" for k in ("kwh_exceeded", "session_renew", "max_amp", "panel_open",
                                             "no_voltage", "disconnected", "reconnected")}
    t.post(f"/meters/{a}/notifications", data={**all_on, "notify_disconnected_min": "5"})
    with app.app_context():
        d = db.session.get(Device, a)
        conn = FakeConn()
        ingest.handle(conn, d, {"t": "evt", "seq": 1, "ts": 1790000000, "tv": 1, "e": "tamper_open"})
        ingest.handle(conn, d, {"t": "evt", "seq": 2, "ts": 1790000010, "tv": 1, "e": "quota", "kwh": 2.0})
        admin_id = User.query.filter_by(username="admin").one().id
        viewer_id = User.query.filter_by(username="tenant1").one().id
        assert {n.type for n in Notification.query.filter_by(user_fk=admin_id)} == {"panel_open"}
        assert {n.type for n in Notification.query.filter_by(user_fk=viewer_id)} == {"panel_open", "kwh_exceeded"}
    # each user only sees their own list
    assert b"Session kWh limit" in t.get("/notifications").data
    assert b"Session kWh limit" not in admin.get("/notifications").data


def test_unlinked_viewer_gets_no_notifications(app, admin):
    with app.app_context():
        a = _meter("Dryer A", "AAAA1111")
    _viewer(admin, "tenant1", [a])
    t = _login(app, "tenant1")
    t.post(f"/meters/{a}/notifications", data={"notify_panel_open": "on", "notify_disconnected_min": "5"})
    with app.app_context():
        uid = User.query.filter_by(username="tenant1").one().id
    admin.post("/settings/users", data={"what": "access", "user_id": uid})  # unlink everything
    with app.app_context():
        assert NotificationRule.query.filter_by(user_fk=uid).count() == 0
        d = db.session.get(Device, a)
        ingest.handle(FakeConn(), d, {"t": "evt", "seq": 1, "ts": 1790000000, "tv": 1, "e": "tamper_open"})
        assert Notification.query.filter_by(user_fk=uid).count() == 0


def test_viewer_lockout_and_unlock(app, admin):
    _viewer(admin, "tenant1")
    with app.app_context():
        settings.set_many({"login_max_attempts": "3", "login_lock_minutes": "0"})
    c = app.test_client()
    for _ in range(3):
        c.post("/login", data={"username": "tenant1", "password": "wrong"})
    r = c.post("/login", data={"username": "tenant1", "password": VPW})
    assert b"locked" in r.data
    with app.app_context():
        u = User.query.filter_by(username="tenant1").one()
        assert u.locked_until == -1
        uid = u.id
    admin.post("/settings/users", data={"what": "unlock", "user_id": uid})
    r = c.post("/login", data={"username": "tenant1", "password": VPW})
    assert r.status_code == 302


def test_admin_is_never_locked(app, admin):
    with app.app_context():
        settings.set_many({"login_max_attempts": "2"})
    c = app.test_client()
    for _ in range(4):
        c.post("/login", data={"username": "admin", "password": "wrong"})
    assert c.post("/login", data={"username": "admin", "password": ADMIN_PW}).status_code == 302


def test_bulk_disable_all_logs_viewers_out(app, admin):
    _viewer(admin, "tenant1")
    _viewer(admin, "tenant2")
    t = _login(app, "tenant1")
    assert t.get("/").status_code == 200
    admin.post("/settings/users", data={"what": "bulk", "scope": "all", "action": "disable"})
    assert t.get("/").status_code == 302
    with app.app_context():
        assert User.query.filter_by(role="viewer", is_enabled=True).count() == 0
        assert User.query.filter_by(role="admin", is_enabled=True).count() == 1


def test_out_of_range_readings_are_dropped(app):
    with app.app_context():
        a = _meter("Dryer A", "AAAA1111")
        d = db.session.get(Device, a)
        ingest.handle(FakeConn(), d, {"t": "tel", "items": [
            {"seq": 1, "ts": 1790000000, "tv": 1, "ch": [{"i": 0, "v": 9999, "a": 5, "w": 1150, "pf": 3, "e": 1}]}]})
        from app.models import Measurement
        m = Measurement.query.one()
        assert m.v is None and m.pf is None and m.a == 5
