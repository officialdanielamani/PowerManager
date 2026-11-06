import re

from app.extensions import db
from app.models import (Device, Event, Incident, Measurement, Notification, Session, User, now)
from app.services import ingest, scheduler

from .conftest import FakeConn
from .test_access import _meter, _viewer
from .test_ingest import tel


def _pair(admin, device_id="MTR23H4R"):
    html = admin.get("/meters/add").get_data(as_text=True).replace("&#34;", '"')
    code = re.search(r'"c":"([A-Z0-9]{8})"', html).group(1)
    return admin.post("/api/v1/pair", json={"code": code, "device_id": device_id, "caps": {"channels": 1}})


def _live_meter(app, pk):
    """Give a meter a running session, a reading and an ongoing Offline + No power incident."""
    with app.app_context():
        d = db.session.get(Device, pk)
        conn = FakeConn()
        t = now() - 300
        ingest.handle(conn, d, {"t": "evt", "seq": 1, "ts": t, "tv": 1, "e": "session_start", "sid": 1})
        ingest.handle(conn, d, {"t": "state", "sess": {"sid": 1, "kwh": 0.5, "status": "active"}})
        ingest.handle(conn, d, {"t": "tel", "items": [tel(2, t + 10, 1.0)]})
        ingest.handle(conn, d, {"t": "evt", "seq": 3, "ts": t + 20, "tv": 1, "e": "tamper_open"})
        db.session.commit()


def test_disable_is_read_only_and_enable(app, admin):
    with app.app_context():
        pk = _meter("Dryer A", "AAAA1111")
    _live_meter(app, pk)
    assert admin.post(f"/meters/{pk}/action", json={"action": "disable"}).get_json()["ok"]
    with app.app_context():
        d = db.session.get(Device, pk)
        assert d.disabled_at and Session.query.filter_by(device_fk=pk, sid=1).one().end_reason == "disabled"
        assert Incident.query.filter_by(device_fk=pk, ended_at=None).count() == 0  # ongoing events ended
        n = Measurement.query.filter_by(device_fk=pk).count()
        conn = FakeConn()
        ingest.handle(conn, d, {"t": "tel", "items": [tel(9, now(), 2.0)]})  # read-only: not stored, but acked
        assert Measurement.query.filter_by(device_fk=pk).count() == n and conn.sent[-1]["last_seq"] == 9
        ingest.on_disconnect(d, "connection_lost")                          # no Offline incident
        assert Incident.query.filter_by(device_fk=pk, type="offline").count() == 0
    r = admin.post(f"/meters/{pk}/action", json={"action": "relay_on"}).get_json()
    assert r["error"] == "disabled"
    assert admin.get(f"/meters/{pk}/configure").status_code == 302   # configuration is read-only
    assert admin.post(f"/meters/{pk}/action", json={"action": "enable"}).get_json()["ok"]
    with app.app_context():
        assert db.session.get(Device, pk).disabled_at is None


def test_unsync_keeps_data_and_resync_choice(app, admin):
    assert _pair(admin).status_code == 200
    with app.app_context():
        pk = Device.query.filter_by(device_id="MTR23H4R").one().id
        db.session.get(Device, pk).name = "Dryer A"
        db.session.commit()
    _live_meter(app, pk)
    assert admin.post(f"/meters/{pk}/action", json={"action": "unsync"}).get_json()["ok"]
    with app.app_context():
        d = db.session.get(Device, pk)
        assert d.removed_at and Measurement.query.filter_by(device_fk=pk).count() == 1
        assert Incident.query.filter_by(device_fk=pk, ended_at=None).count() == 0
    # same hardware pairs again -> the admin is asked; choose "new meter"
    assert _pair(admin).status_code == 200
    assert b"was used before" in admin.get(f"/meters/{pk}/configure?new=1").data
    admin.post(f"/meters/{pk}/resync", data={"choice": "new"})
    with app.app_context():
        live = db.session.get(Device, pk)
        old = Device.query.filter_by(device_id="MTR23H4R~1").one()
        assert live.name == "MTR23H4R" and not live.resync_pending and live.removed_at is None
        assert old.name == "Dryer A" and old.removed_at
        assert Measurement.query.filter_by(device_fk=pk).count() == 0
        assert Measurement.query.filter_by(device_fk=old.id).count() == 1


def test_resync_continue_keeps_history(app, admin):
    _pair(admin)
    with app.app_context():
        pk = Device.query.filter_by(device_id="MTR23H4R").one().id
    _live_meter(app, pk)
    admin.post(f"/meters/{pk}/action", json={"action": "unsync"})
    _pair(admin)
    admin.post(f"/meters/{pk}/resync", data={"choice": "continue"})
    with app.app_context():
        assert not db.session.get(Device, pk).resync_pending
        assert Measurement.query.filter_by(device_fk=pk).count() == 1


def test_remove_needs_device_id_and_deletes_everything(app, admin):
    with app.app_context():
        pk = _meter("Dryer A", "AAAA1111")
    _live_meter(app, pk)
    r = admin.post(f"/meters/{pk}/action", json={"action": "remove", "confirm": "nope"})
    assert r.status_code == 400
    assert admin.post(f"/meters/{pk}/action", json={"action": "remove", "confirm": "aaaa1111"}).get_json()["ok"]
    with app.app_context():
        d = db.session.get(Device, pk)
        assert d.purged and d.removed_at
        for model in (Measurement, Session, Incident, Event):
            assert model.query.filter_by(device_fk=pk).count() == 0
    assert admin.get(f"/meters/{pk}").status_code == 404
    assert b"AAAA1111" not in admin.get("/").data  # not listed under unsynced meters


def test_power_and_offline_together_closes_no_power(app):
    with app.app_context():
        pk = _meter("Dryer A", "AAAA1111")
        d = db.session.get(Device, pk)
        conn = FakeConn()
        ingest.on_connect(d, conn)
        ingest.handle(conn, d, {"t": "evt", "seq": 1, "ts": now() - 20, "tv": 1, "e": "ac_lost"})
        ingest.on_disconnect(d, "connection_lost")  # 20 s later: the whole meter lost power
        pwr = Incident.query.filter_by(device_fk=pk, type="power").one()
        off = Incident.query.filter_by(device_fk=pk, type="offline").one()
        assert pwr.ended_at is not None and pwr.detail["closed_by"] == "offline"
        assert off.ended_at is None and off.detail["power_lost"]


def test_no_power_long_before_offline_stays_open(app):
    with app.app_context():
        pk = _meter("Dryer A", "AAAA1111")
        d = db.session.get(Device, pk)
        conn = FakeConn()
        ingest.handle(conn, d, {"t": "evt", "seq": 1, "ts": now() - 600, "tv": 1, "e": "ac_lost"})
        ingest.on_disconnect(d, "connection_lost")
        assert Incident.query.filter_by(device_fk=pk, type="power").one().ended_at is None


def test_delete_events_and_notifications(app, admin):
    with app.app_context():
        pk = _meter("Dryer A", "AAAA1111")
    _live_meter(app, pk)
    with app.app_context():
        ids = [i.id for i in Incident.query.filter_by(device_fk=pk)]
        uid = User.query.filter_by(username="admin").one().id
        for k in range(3):
            db.session.add(Notification(device_fk=pk, user_fk=uid, type="panel_open", message=f"n{k}"))
        db.session.commit()
        nids = [n.id for n in Notification.query.filter_by(user_fk=uid)]
    assert admin.post(f"/meters/{pk}/incidents/delete", json={"ids": ids}).get_json()["deleted"] == len(ids)
    assert admin.post("/notifications/delete", json={"ids": nids[:1]}).get_json()["deleted"] == 1
    assert admin.post("/notifications/delete", json={"all": True}).get_json()["deleted"] == 2


def test_viewer_cannot_delete_or_lifecycle(app, admin):
    with app.app_context():
        pk = _meter("Dryer A", "AAAA1111")
    _viewer(admin, "tenant1", [pk])
    v = app.test_client()
    v.post("/login", data={"username": "tenant1", "password": "Vi3wer!Passw0rd"})
    assert v.post(f"/meters/{pk}/incidents/delete", json={"ids": [1]}).status_code == 403
    assert v.post(f"/meters/{pk}/action", json={"action": "disable"}).status_code == 403
    assert v.get(f"/meters/{pk}/device-info").status_code == 403


def test_cleanup_job(app, admin):
    with app.app_context():
        pk = _meter("Dryer A", "AAAA1111")
        d = db.session.get(Device, pk)
        d.data_autodel, d.data_autodel_days = True, 120
        d.sessions_autodel, d.sessions_autodel_days = True, 120  # separate option since 0.7.3
        d.events_autodel, d.events_autodel_days = True, 120
        u = User.query.filter_by(username="admin").one()
        u.notif_autodel, u.notif_autodel_days = True, 30
        old, new = now() - 200 * 86400, now() - 86400
        db.session.add_all([
            Measurement(device_fk=pk, ch=0, seq=1, ts=old, w=1), Measurement(device_fk=pk, ch=0, seq=2, ts=new, w=1),
            Session(device_fk=pk, sid=1, started_at=old, ended_at=old + 60),
            Incident(device_fk=pk, type="tamper", started_at=old, ended_at=old + 5),
            Notification(device_fk=pk, user_fk=u.id, type="x", message="old", ts=now() - 40 * 86400),
            Notification(device_fk=pk, user_fk=u.id, type="x", message="new", ts=new)])
        db.session.commit()
        scheduler.cleanup()
        assert Measurement.query.filter_by(device_fk=pk).count() == 1
        assert Session.query.filter_by(device_fk=pk).count() == 0
        assert Incident.query.filter_by(device_fk=pk).count() == 0
        assert [n.message for n in Notification.query] == ["new"]


def test_meter_info_is_stored(app):
    with app.app_context():
        pk = _meter("Dryer A", "AAAA1111")
        d = db.session.get(Device, pk)
        ingest.handle(FakeConn(), d, {"t": "info", "ip": "192.168.1.57", "heap": 180000, "bat_v": "4.05",
                                      "bat_pct": 83, "hw_info": "PZEM-004T 1Chn", "evil": "x"})
        assert d.last_info["ip"] == "192.168.1.57" and "evil" not in d.last_info and d.last_info_at


def test_telegram_token_is_write_only(app, admin):
    from app.services import settings
    page = admin.get("/settings/?tab=notifications").get_data(as_text=True)
    admin.post("/settings/notifications", data={"telegram_enabled": "on", "telegram_bot_token": "123:SECRET"})
    assert settings.get("telegram_bot_token") == "123:SECRET"
    page = admin.get("/settings/?tab=notifications").get_data(as_text=True)
    assert "123:SECRET" not in page and "saved" in page
    admin.post("/settings/notifications", data={"telegram_enabled": "on", "telegram_bot_token": ""})
    assert settings.get("telegram_bot_token") == "123:SECRET"  # empty = keep
    admin.post("/settings/notifications", data={"telegram_bot_token": "", "telegram_clear": "on"})
    assert settings.get("telegram_bot_token") == ""


def test_meter_status_notification_default_on(app, admin):
    from app.models import Device, Notification, NotificationRule
    from app.routes.device_api import ensure_device_defaults
    from app.extensions import db
    with app.app_context():
        d = Device(device_id="STAT0001", name="Pump", caps={"channels": 1}, cfg={}, pair_gen=1)
        db.session.add(d)
        db.session.flush()
        ensure_device_defaults(d, reset=True)
        db.session.commit()
        pk = d.id
    # no saved choice: on by default
    html = admin.get(f"/meters/{pk}/notifications").get_data(as_text=True)
    assert 'name="notify_meter_status"' in html and "Meter status" in html
    assert admin.post(f"/meters/{pk}/action", json={"action": "disable"}).get_json()["ok"]
    assert admin.post(f"/meters/{pk}/action", json={"action": "enable"}).get_json()["ok"]
    with app.app_context():
        msgs = [n.message for n in Notification.query.filter_by(type="meter_status").order_by(Notification.id)]
        assert len(msgs) == 2 and "disabled" in msgs[0] and "enabled" in msgs[1]
        # switched off by the user: no more
        db.session.add(NotificationRule(device_fk=pk, user_fk=1, type="meter_status", enabled=False))
        db.session.commit()
    admin.post(f"/meters/{pk}/action", json={"action": "disable"})
    with app.app_context():
        assert Notification.query.filter_by(type="meter_status").count() == 2


def test_remove_keeps_meter_status_notification(app, admin):
    from app.models import Device, Notification
    from app.routes.device_api import ensure_device_defaults
    from app.extensions import db
    with app.app_context():
        d = Device(device_id="STAT0002", name="Heater", caps={"channels": 1}, cfg={}, pair_gen=1)
        db.session.add(d)
        db.session.flush()
        ensure_device_defaults(d, reset=True)
        db.session.commit()
        pk = d.id
    r = admin.post(f"/meters/{pk}/action", json={"action": "remove", "confirm": "STAT0002"})
    assert r.get_json()["ok"], r.get_json()
    with app.app_context():
        n = Notification.query.filter_by(type="meter_status").one()
        assert n.message.startswith("[Heater]") and "removed" in n.message


def test_sessions_delete_only_ended(app, admin):
    from app.models import Device, Session
    from app.extensions import db
    with app.app_context():
        d = Device(device_id="SES00001", name="S", caps={"channels": 1}, cfg={}, pair_gen=1)
        db.session.add(d)
        db.session.flush()
        a = Session(device_fk=d.id, gen=1, sid=1, started_at=100, ended_at=200)
        b = Session(device_fk=d.id, gen=1, sid=2, started_at=200)  # running
        db.session.add_all([a, b])
        db.session.commit()
        pk, ids = d.id, [a.id, b.id]
    r = admin.post(f"/meters/{pk}/sessions/delete", json={"ids": ids}).get_json()
    assert r["ok"] and r["deleted"] == 1
    with app.app_context():
        assert [s.sid for s in Session.query.filter_by(device_fk=pk)] == [2]


def test_cleanup_options_are_separate(app, admin):
    from app.extensions import db
    from app.models import Device, Measurement, Session, now
    from app.services import scheduler
    with app.app_context():
        d = Device(device_id="CLN00001", name="C", caps={"channels": 1}, cfg={}, pair_gen=1)
        db.session.add(d)
        db.session.flush()
        old = now() - 200 * 86400
        db.session.add(Measurement(device_fk=d.id, ch=0, gen=1, seq=1, ts=old, w=1.0))
        db.session.add(Session(device_fk=d.id, gen=1, sid=1, started_at=old, ended_at=old + 10))
        d.data_autodel, d.data_autodel_days = True, 120  # Historical Data only
        db.session.commit()
        scheduler.cleanup()
        assert Measurement.query.filter_by(device_fk=d.id).count() == 0
        assert Session.query.filter_by(device_fk=d.id).count() == 1  # sessions kept: their own option is off


def test_delete_history_range_last_all(app, admin):
    from app.extensions import db
    from app.models import Device, Measurement, Rollup, Session, now
    with app.app_context():
        d = Device(device_id="DEL00001", name="D", caps={"channels": 1}, cfg={}, pair_gen=1)
        db.session.add(d)
        db.session.flush()
        t = now()
        for k, age in enumerate((60, 2 * 3600, 3 * 86400)):  # 1 min, 2 h, 3 days old
            db.session.add(Measurement(device_fk=d.id, ch=0, gen=1, seq=k + 1, ts=t - age, w=10.0, e=1.0 + k))
        db.session.add(Rollup(device_fk=d.id, ch=0, period="d", bucket_start=(t - 3 * 86400) - (t - 3 * 86400) % 86400, n=1))
        db.session.add(Session(device_fk=d.id, gen=1, sid=1, started_at=t - 4 * 86400))
        db.session.commit()
        pk = d.id
    url = f"/meters/{pk}/history/delete"
    r = admin.post(url, json={"mode": "last", "n": 10, "unit": "minutes"}).get_json()
    assert r["ok"] and r["deleted"] == 1
    r = admin.post(url, json={"mode": "all"})  # not confirmed
    assert r.status_code == 400
    r = admin.post(url, json={"mode": "all", "confirm": True}).get_json()
    assert r["ok"] and r["deleted"] == 2
    with app.app_context():
        assert Measurement.query.filter_by(device_fk=pk).count() == 0
        assert Rollup.query.filter_by(device_fk=pk).count() == 0
        assert Session.query.filter_by(device_fk=pk).count() == 1  # sessions are not Historical Data
