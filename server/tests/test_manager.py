"""Manager accounts: own meters, own view-only accounts, owner transfer; per-channel viewers."""
from app.extensions import db
from app.models import Device, MeterViewer, User, now
from app.services import ingest

from .conftest import FakeConn
from .test_access import VPW, _login, _meter
from .test_multichannel import _pair_mc
from .test_pairing import _code, _pair

MPW = "Man4ger!Passw0rd"


def _account(app, client, name, role="viewer", meters=(), view_all=False, manager=None):
    """(No app context may be pushed around a request: flask-login would reuse the cached user.)"""
    data = {"what": "add", "username": name, "password": MPW if role == "manager" else VPW,
            "confirm": MPW if role == "manager" else VPW, "role": role, "meters": [str(m) for m in meters]}
    if view_all:
        data["view_all"] = "on"
    if manager:
        data["manager"] = str(manager)
    r = client.post("/settings/users", data=data)
    assert r.status_code == 302, r.data
    with app.app_context():
        return User.query.filter_by(username=name).one().id


def _cfg_form(name, **kw):
    f = {"name": name, "info": "", "report_s": "60", "read_s": "60", "sess_mode": "none", "kwh_max_0": "0",
         "amp_max_0": "0", "daily_at": "00:00", "price_per_kwh": "0.25", "positive_only": "on"}
    f.update(kw)
    return f


def _names(c):
    return sorted(m["name"] for m in c.get("/api/ui/meters").get_json()["meters"])


def _setup(app, admin):
    """Admin: House-A, House-B; House-A given to manager adam; adam pairs My-Kitchen."""
    with app.app_context():
        a, b = _meter("House-A", "AAAA1111"), _meter("House-B", "BBBB2222")
    adam = _account(app, admin, "adam", "manager")
    r = admin.post(f"/meters/{a}/configure", data=_cfg_form("House-A", owner=str(adam)))
    assert r.status_code in (200, 302)
    m = _login(app, "adam", MPW)
    assert _pair(m, _code(m), "KITCHEN1").status_code == 200
    with app.app_context():
        k = Device.query.filter_by(device_id="KITCHEN1").one()
        assert k.owner_fk == adam
        k.name = "My-Kitchen"
        db.session.commit()
        return a, b, k.id, adam, m


def test_manager_sees_and_manages_own_meters(app, admin):
    a, b, k, adam, m = _setup(app, admin)
    assert _names(m) == ["House-A", "My-Kitchen"]
    assert _names(admin) == ["House-A", "House-B", "My-Kitchen"]
    assert m.get(f"/meters/{a}/configure").status_code == 200
    assert m.get(f"/meters/{k}/configure").status_code == 200
    assert m.get(f"/meters/{b}").status_code == 404
    assert m.get(f"/meters/{b}/configure").status_code == 404
    assert m.post(f"/meters/{b}/action", json={"action": "renew"}).status_code == 404
    assert m.post(f"/meters/{a}/action", json={"action": "renew"}).get_json()["ok"]
    assert b"Configuration" in m.get(f"/meters/{a}").data
    # server settings stay admin-only
    assert m.post("/settings/server", data={"timezone": "UTC"}).status_code == 403
    assert m.post("/settings/notifications", data={}).status_code == 403
    html = m.get("/settings/?tab=server").get_data(as_text=True)
    assert "server_address" not in html
    # the manager deletes its own meter
    r = m.post(f"/meters/{k}/action", json={"action": "remove", "confirm": "KITCHEN1"})
    assert r.get_json()["ok"]
    assert _names(m) == ["House-A"]


def test_manager_viewers_are_tied_to_the_manager(app, admin):
    a, b, k, adam, m = _setup(app, admin)
    v1 = _account(app, m, "vone", meters=[a, b])  # b is not adam's: ignored
    _account(app, m, "vtwo", view_all=True)
    own = _account(app, admin, "own", meters=[b])
    with app.app_context():
        assert db.session.get(User, v1).manager_fk == adam
        assert {x.device_fk for x in MeterViewer.query.filter_by(user_fk=v1)} == {a}
    assert _names(_login(app, "vone")) == ["House-A"]
    assert _names(_login(app, "vtwo")) == ["House-A", "My-Kitchen"]  # view all = all of adam's
    # the manager lists and manages only its own viewers
    html = m.get("/settings/?tab=users").get_data(as_text=True)
    assert "vone" in html and "vtwo" in html and ">own<" not in html
    assert m.post("/settings/users", data={"what": "toggle", "user_id": str(own)}).status_code == 404
    assert m.post("/settings/users", data={"what": "add", "username": "boss", "password": MPW, "confirm": MPW,
                                           "role": "manager"}).status_code == 302
    with app.app_context():
        assert User.query.filter_by(username="boss").one().role == "viewer"  # managers cannot make managers
    # the admin can bypass: link adam's viewer to the admin's meter
    admin.post("/settings/users", data={"what": "access", "user_id": str(v1), "manager": str(adam),
                                        "meters": [str(a), str(b)]})
    assert _names(_login(app, "vone")) == ["House-A", "House-B"]
    # the manager saving v1's access keeps the admin's link
    m.post("/settings/users", data={"what": "access", "user_id": str(v1), "meters": [str(a)]})
    assert _names(_login(app, "vone")) == ["House-A", "House-B"]


def test_admin_transfers_meter(app, admin):
    a, b, k, adam, m = _setup(app, admin)
    v1 = _account(app, m, "vone", meters=[a])
    bob = _account(app, admin, "bob", "manager")
    admin.post(f"/meters/{a}/configure", data=_cfg_form("House-A", owner=str(bob), viewers=[]))
    with app.app_context():
        assert db.session.get(Device, a).owner_fk == bob
        assert MeterViewer.query.filter_by(user_fk=v1).count() == 0  # adam's viewer lost the meter
    assert _names(m) == ["My-Kitchen"]
    assert _names(_login(app, "bob", MPW)) == ["House-A"]
    # a manager cannot change the owner
    bm = _login(app, "bob", MPW)
    bm.post(f"/meters/{a}/configure", data=_cfg_form("House-A", owner=""))
    with app.app_context():
        assert db.session.get(Device, a).owner_fk == bob
    # back to the admin
    admin.post(f"/meters/{a}/configure", data=_cfg_form("House-A", owner=""))
    with app.app_context():
        assert db.session.get(Device, a).owner_fk is None


def test_delete_manager_returns_meters_to_admin(app, admin):
    a, b, k, adam, m = _setup(app, admin)
    v1 = _account(app, m, "vone", meters=[a])
    assert admin.post("/settings/users", data={"what": "delete", "user_id": str(adam)}).status_code == 302
    with app.app_context():
        assert db.session.get(Device, a).owner_fk is None and db.session.get(Device, k).owner_fk is None
        assert db.session.get(User, v1).manager_fk is None


def test_per_meter_price_in_configuration(app, admin):
    with app.app_context():
        a = _meter("House-A", "AAAA1111")
    admin.post(f"/meters/{a}/configure", data=_cfg_form("House-A", currency="myr", price_per_kwh="0.218"))
    with app.app_context():
        d = db.session.get(Device, a)
        assert d.currency == "MYR" and d.price_per_kwh == 0.218 and d.positive_only
    admin.post(f"/meters/{a}/configure", data=_cfg_form("House-A", price_per_kwh="", positive_only=""))
    with app.app_context():
        d = db.session.get(Device, a)
        assert d.price_per_kwh is None and not d.positive_only


def test_viewer_limited_to_channels(app, admin):
    assert _pair_mc(admin, _code(admin)).status_code == 200
    uid = _account(app, admin, "tenantb")
    with app.app_context():
        d = Device.query.filter_by(device_id="MCM00001").one()
        pk = d.id
        ingest.handle(FakeConn(), d, {"t": "tel", "items": [{"seq": 5, "ts": now() - 5, "tv": 1, "ch": [
            {"i": 0, "v": 230, "a": 1.0, "w": 200, "pf": 0.9, "e": 1.0},
            {"i": 1, "v": 230, "a": 2.0, "w": 400, "pf": 0.9, "e": 2.0}]}]})
        d.last_state = {"sess": {"sid": 1, "kwh": 3.0, "status": "active"}}
        db.session.commit()
    form = {"name": "Panel", "report_s": "60", "read_s": "60", "sess_mode": "none", "daily_at": "00:00",
            "price_per_kwh": "0.25", "viewers": [str(uid)], f"vch_set_{uid}": "1", f"vch_{uid}": ["1"]}
    assert admin.post(f"/meters/{pk}/configure", data=form).status_code in (200, 302)
    with app.app_context():
        assert db.session.get(MeterViewer, (pk, uid)).channels == [1]
    t = _login(app, "tenantb")
    live = t.get(f"/meters/{pk}/live").get_json()
    assert [c["i"] for c in live["channels"]] == [1]
    assert live["w"] == 400 and live["kwh"] is None  # shared session total hidden
    assert t.get(f"/meters/{pk}/history?ch=0").status_code == 404
    assert t.get(f"/meters/{pk}/history?ch=1").status_code == 200
    with app.app_context():
        db.session.get(User, uid).can_csv = True
        db.session.commit()
    header = t.get(f"/meters/{pk}/export.csv?range=1d").get_data(as_text=True).splitlines()[0]
    assert "CH2" in header and "CH1" not in header
    # the admin still sees everything
    assert [c["i"] for c in admin.get(f"/meters/{pk}/live").get_json()["channels"]] == [0, 1]

