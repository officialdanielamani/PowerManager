"""Different hardware / firmware type on the same ESP32: old history archived as <ID>~n, live meter clean."""
from app.extensions import db
from app.models import Device, Measurement, MeterViewer, Notification, NotificationRule, Session, User, now
from app.services import devconfig, ingest

from .conftest import FakeConn
from .test_pairing import _code

AC = {"kind": "ac", "sensor": "pzem004t", "channels": 1, "relays": 1}
MC2 = {"kind": "ac", "mc": True, "sensor": "rbamp", "channels": 2, "relays": 2, "ratings": [30, 30]}


def _hello(device, caps, up=100):
    ingest.handle(FakeConn(), device, {"t": "hello", "up": up, "boot": 1, "cfg_ver": 0, "caps": caps})


def test_signature():
    assert devconfig.hw_signature(AC) == "ac-1"
    assert devconfig.hw_signature({"kind": "dc"}) == "dc-1"
    assert devconfig.hw_signature(MC2) == "ac-mc2"
    assert devconfig.hw_signature({"kind": "ac", "mc": True}) is None  # module not found yet: unknown


def test_hardware_change_archives_history(app, admin):
    r = admin.post("/api/v1/pair", json={"code": _code(admin), "device_id": "HWC00001", "fw": "0.6", "proto": 1, "caps": AC})
    assert r.status_code == 200
    with app.app_context():
        d = Device.query.filter_by(device_id="HWC00001").one()
        assert d.hw_sig == "ac-1"
        d.name = "Workshop"
        viewer = User(username="vee", role="viewer")
        viewer.set_password("Str0ng!Passw0rd")
        db.session.add(viewer)
        db.session.flush()
        db.session.add(MeterViewer(device_fk=d.id, user_fk=viewer.id))
        db.session.add(NotificationRule(device_fk=d.id, user_fk=viewer.id, type="max_amp", enabled=True))
        db.session.commit()
        conn, t = FakeConn(), now() - 100
        ingest.handle(conn, d, {"t": "evt", "seq": 1, "ts": t, "tv": 1, "e": "session_start", "sid": 1, "max": 5})
        ingest.handle(conn, d, {"t": "tel", "items": [{"seq": 2, "ts": t + 10, "tv": 1,
                               "ch": [{"i": 0, "v": 230, "a": 1, "w": 230, "pf": 1, "hz": 50, "e": 3.0}]}]})
        gen = d.pair_gen

        _hello(d, AC)  # same hardware: nothing happens
        assert Device.query.filter(Device.device_id.like("HWC00001~%")).count() == 0

        _hello(d, MC2)  # rbAmp 2-channel firmware flashed on the same ESP32
        arch = Device.query.filter_by(device_id="HWC00001~1").one()
        assert arch.removed_at and arch.hw_sig == "ac-1" and arch.last_state["archive"]["to"] == "AC multi-channel, 2 ch"
        assert Measurement.query.filter_by(device_fk=arch.id).count() == 1
        assert Session.query.filter_by(device_fk=arch.id).count() == 1
        assert MeterViewer.query.filter_by(device_fk=arch.id, user_fk=viewer.id).count() == 1  # still sees old data
        # live meter: same name / viewers / notification options, new type, clean history and config
        assert d.name == "Workshop" and d.is_mc and d.hw_sig == "ac-mc2" and d.pair_gen == gen + 1
        assert d.cfg["mode"] == "shared" and len(d.cfg["ch"]) == 2 and len(d.channels) == 2
        assert Measurement.query.filter_by(device_fk=d.id).count() == 0
        assert MeterViewer.query.filter_by(device_fk=d.id).count() == 1
        assert NotificationRule.query.filter_by(device_fk=d.id).count() == 1
        n = Notification.query.filter_by(type="meter_status").all()
        assert any("Hardware changed: AC (PZEM-004T) → AC multi-channel, 2 ch" in x.message for x in n)

        _hello(d, {"kind": "ac", "mc": True})  # module missing at boot: channel count unknown -> no change
        assert Device.query.filter(Device.device_id.like("HWC00001~%")).count() == 1

        _hello(d, {"kind": "dc", "sensor": "pzem017", "channels": 1, "mc": False})  # then a DC meter
        assert Device.query.filter_by(device_id="HWC00001~2").count() == 0  # no history yet on the 2ch: no archive
        assert d.kind == "dc" and not d.is_mc
        arch_id = arch.id
    html = admin.get(f"/meters/{arch_id}").get_data(as_text=True)
    assert "the hardware of this meter changed" in html
