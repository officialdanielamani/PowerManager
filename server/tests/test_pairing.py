import re

from app.extensions import db
from app.models import Device, PairingCode, now


def _code(admin):
    html = admin.get("/meters/add").get_data(as_text=True).replace("&#34;", '"')
    return re.search(r'"c":"([A-Z0-9]{8})"', html).group(1)


def _pair(client, code, device_id="MTR23H4R"):
    return client.post("/api/v1/pair", json={"code": code, "device_id": device_id, "fw": "0.1", "proto": 1,
                                              "caps": {"channels": 1, "relays": 1}})


def test_pair_success_and_single_use(app, admin):
    code = _code(admin)
    r = _pair(admin, code[:4] + "-" + code[4:])  # dash accepted
    assert r.status_code == 200
    body = r.get_json()
    assert body["secret"] and body["ws"] == "/ws/device" and body["config"]["ver"] == 1
    assert admin.get(f"/meters/pairing/{code}/status").get_json()["status"] == "paired"
    assert _pair(admin, code, "OTHER123").status_code == 404  # used


def test_pair_expired_and_conflict(app, admin):
    code = _code(admin)
    with app.app_context():
        db.session.get(PairingCode, code).expires_at = now() - 1
        db.session.commit()
    assert _pair(admin, code).status_code == 404
    assert _pair(admin, _code(admin)).status_code == 200
    assert _pair(admin, _code(admin)).status_code == 409  # already paired


def test_repair_after_remove_bumps_generation(app, admin):
    assert _pair(admin, _code(admin)).status_code == 200
    with app.app_context():
        pk = Device.query.first().id
    r = admin.post(f"/meters/{pk}/action", json={"action": "unsync"})
    assert r.get_json()["ok"]
    assert _pair(admin, _code(admin)).status_code == 200
    with app.app_context():
        d = db.session.get(Device, pk)
        assert d.removed_at is None and d.pair_gen == 2 and d.resync_pending


def test_bad_requests(admin):
    assert admin.post("/api/v1/pair", data="nope").status_code == 400
    assert _pair(admin, "ABCDEFGH", "bad id!").status_code == 400
