import json
import re
import time

from app.extensions import db
from app.models import Device, Incident, Measurement, now
from app.services import hub, mesh

from .test_access import _meter
from .test_ingest import tel


class GwConn:
    """Stands in for the gateway's hub.Connection."""
    direct = True

    def __init__(self, pk):
        self.device_pk = pk
        self.sent = []

    def send(self, msg):
        self.sent.append(msg)


def _setup(app):
    with app.app_context():
        gw_pk = _meter("Gateway", "GATEWAY1")
        leaf_pk = _meter("Leaf", "LEAF0001")
        leaf = db.session.get(Device, leaf_pk)
        leaf.link_key = "k" * 32
        db.session.commit()
    return gw_pk, leaf_pk


def _via(n, msg, key="k" * 32, dev="LEAF0001"):
    m = json.dumps(msg)
    return {"t": "via", "d": dev, "n": n, "h": mesh.sign(key, dev, n, m), "m": m}


def _unwrap(out, key="k" * 32):
    """Check the server's signature on a downlink message and return the inner message."""
    assert out["t"] == "via" and out["h"] == mesh.sign(key, out["d"], out["n"], out["m"])
    return json.loads(out["m"])


def test_leaf_messages_through_gateway(app):
    gw_pk, leaf_pk = _setup(app)
    with app.app_context():
        gw = db.session.get(Device, gw_pk)
        gconn = GwConn(gw_pk)
        assert mesh.handle(gconn, gw, _via(1, {"t": "hello", "up": 5, "cfg_ver": 0}))
        inner = [_unwrap(o) for o in gconn.sent]
        assert inner[0]["t"] == "welcome" and any(i["t"] == "cfg" for i in inner)
        assert not any(i["t"] == "link_key" for i in inner)  # never sent through a gateway
        assert hub.is_online(leaf_pk) and db.session.get(Device, leaf_pk).via_fk == gw_pk
        mesh.handle(gconn, gw, _via(2, {"t": "tel", "items": [tel(1, now() - 5, 1.0)]}))
        assert Measurement.query.filter_by(device_fk=leaf_pk).count() == 1
        assert _unwrap(gconn.sent[-1])["t"] == "ack"
        # the server can talk to the leaf like any other meter (signed downlink)
        hub.send(leaf_pk, {"t": "cmd", "c": "get_state", "ref": 9})
        assert _unwrap(gconn.sent[-1])["c"] == "get_state"
        mesh.gateway_gone(gconn)
        assert not hub.is_online(leaf_pk)
        assert Incident.query.filter_by(device_fk=leaf_pk, type="offline", ended_at=None).count() == 1


def test_forged_and_replayed_messages_are_dropped(app):
    gw_pk, leaf_pk = _setup(app)
    with app.app_context():
        gw = db.session.get(Device, gw_pk)
        gconn = GwConn(gw_pk)
        forged = _via(5, {"t": "tel", "items": [tel(1, now(), 1.0)]}, key="x" * 32)  # gateway without the key
        mesh.handle(gconn, gw, forged)
        changed = _via(6, {"t": "tel", "items": [tel(1, now(), 1.0)]})
        changed["m"] = changed["m"].replace("100.0", "999.0")                       # tampered on the way
        mesh.handle(gconn, gw, changed)
        assert Measurement.query.count() == 0 and not hub.is_online(leaf_pk)
        good = _via(7, {"t": "tel", "items": [tel(1, now(), 1.0)]})
        mesh.handle(gconn, gw, good)
        mesh.handle(gconn, gw, good)                                                 # replay
        mesh.handle(gconn, gw, _via(7, {"t": "tel", "items": [tel(2, now(), 1.1)]}))  # reused counter
        assert Measurement.query.count() == 1
        mesh.gateway_gone(gconn)


def test_leaf_timeout(app, monkeypatch):
    gw_pk, leaf_pk = _setup(app)
    with app.app_context():
        gw = db.session.get(Device, gw_pk)
        gconn = GwConn(gw_pk)
        mesh.handle(gconn, gw, _via(1, {"t": "hello", "up": 5, "cfg_ver": 0}))
        gconn.leaves[leaf_pk].last_rx -= mesh.LEAF_TIMEOUT_S + 1
        mesh.sweep(gconn)
        assert not hub.is_online(leaf_pk)


def test_removed_leaf_is_told_to_reset(app):
    gw_pk, leaf_pk = _setup(app)
    with app.app_context():
        leaf = db.session.get(Device, leaf_pk)
        leaf.removed_at = now()
        db.session.commit()
        gconn = GwConn(gw_pk)
        mesh.handle(gconn, db.session.get(Device, gw_pk), _via(1, {"t": "hello", "up": 1}))
        assert _unwrap(gconn.sent[-1]) == {"t": "bye", "code": 4403, "id": 1}


def test_pairing_through_gateway(app, admin):
    with app.app_context():
        gw_pk = _meter("Gateway", "GATEWAY1")
    html = admin.get("/meters/add").get_data(as_text=True).replace("&#34;", '"')
    code = re.search(r'"c":"([A-Z0-9]{8})"', html).group(1)
    with app.app_context():
        gconn = GwConn(gw_pk)
        mesh.handle(gconn, db.session.get(Device, gw_pk),
                    {"t": "via_pair", "body": {"code": code, "device_id": "LEAF0002", "caps": {"channels": 1}}})
        res = gconn.sent[-1]
        assert res["t"] == "via_pair_res" and res["code"] == 200 and res["d"] == "LEAF0002"
        assert len(res["resp"]["link_key"]) == 32 and res["resp"]["secret"]
        assert Device.query.filter_by(device_id="LEAF0002").one().link_key == res["resp"]["link_key"]
