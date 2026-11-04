from app.extensions import db
from app.models import Command, Device, MeterViewer, User

from .test_access import VPW, _meter, _viewer


def _cfg(app, pk):
    with app.app_context():
        return db.session.get(Device, pk).cfg


def test_bulk_renew_queues_for_offline_meters(app, admin):
    with app.app_context():
        a, b = _meter("A", "AAAA1111"), _meter("B", "BBBB2222")
    r = admin.post("/meters/bulk/renew", json={"ids": [a, b]}).get_json()
    assert r["ok"] and r["queued"] == 2
    with app.app_context():
        assert Command.query.filter_by(c="renew").count() == 2


def test_bulk_configure_changes_only_switched_on_sections(app, admin):
    with app.app_context():
        a, b = _meter("A", "AAAA1111"), _meter("B", "BBBB2222")
        da = db.session.get(Device, a)
        da.cfg = {**da.cfg, "report_s": 30, "ch": [{"kwh_max": 5.0, "kwh_action": "warn", "amp_max": 7.0}]}
        db.session.commit()
    assert admin.get(f"/meters/bulk/configure?ids={a},{b}").status_code == 200
    form = {"ids": f"{a},{b}", "apply_kwh": "on", "kwh_max_0": "2", "kwh_action_0": "end",
            "report_s": "999"}  # report_s is sent but its section is off -> ignored
    r = admin.post("/meters/bulk/configure", data=form)
    assert r.status_code == 200 and b"Applied" in r.data
    ca, cb = _cfg(app, a), _cfg(app, b)
    assert ca["ch"][0]["kwh_max"] == 2.0 and ca["ch"][0]["kwh_action"] == "end"
    assert ca["report_s"] == 30 and ca["ch"][0]["amp_max"] == 7.0   # untouched
    assert cb["ch"][0]["kwh_max"] == 2.0 and cb["report_s"] == 60
    with app.app_context():
        assert db.session.get(Device, a).cfg_ver_wanted == 2  # pushed as a new version


def test_bulk_configure_requires_a_section_and_validates(app, admin):
    with app.app_context():
        a = _meter("A", "AAAA1111")
    r = admin.post("/meters/bulk/configure", data={"ids": str(a)})
    assert b"Switch on at least one section" in r.data
    r = admin.post("/meters/bulk/configure", data={"ids": str(a), "apply_report": "on", "report_s": "5"})
    assert b"between 10 and 3600" in r.data


def test_bulk_link_users_add_and_remove(app, admin):
    with app.app_context():
        a, b = _meter("A", "AAAA1111"), _meter("B", "BBBB2222")
    _viewer(admin, "tenant1")
    with app.app_context():
        uid = User.query.filter_by(username="tenant1").one().id
    admin.post("/meters/bulk/configure", data={"ids": f"{a},{b}", "apply_viewers": "on", "viewer_mode": "add", "viewers": [str(uid)]})
    with app.app_context():
        assert MeterViewer.query.filter_by(user_fk=uid).count() == 2
    admin.post("/meters/bulk/configure", data={"ids": str(a), "apply_viewers": "on", "viewer_mode": "remove", "viewers": [str(uid)]})
    with app.app_context():
        assert {m.device_fk for m in MeterViewer.query.filter_by(user_fk=uid)} == {b}


def test_viewer_cannot_use_bulk(app, admin):
    with app.app_context():
        a = _meter("A", "AAAA1111")
    _viewer(admin, "tenant1", [a])
    v = app.test_client()
    v.post("/login", data={"username": "tenant1", "password": VPW})
    assert v.post("/meters/bulk/renew", json={"ids": [a]}).status_code == 403
    assert v.get(f"/meters/bulk/configure?ids={a}").status_code == 403
    assert b"pm-sel" not in v.get("/").data  # no checkboxes for viewers
