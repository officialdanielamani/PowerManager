"""Notification options: groups, hardware-dependent rows, thresholds, channel limits (independent mode), the session / threshold
notifications and the channel status message."""
import pytest

from app.extensions import db
from app.models import Device, Notification, NotificationRule, now
from app.services import hub, ingest, notify_limits

from .conftest import FakeConn
from .test_multichannel import CAPS, _form, _pair_mc


@pytest.fixture()
def mc(app, admin):
    from .test_pairing import _code
    r = _pair_mc(admin, _code(admin), "NOT00001")
    assert r.status_code == 200
    admin.post("/meters/%d/configure" % _id(app, "NOT00001"), data=_form(mode="independent"))
    with app.app_context():
        yield _id(app, "NOT00001")


def _id(app, did):
    with app.app_context():
        return Device.query.filter_by(device_id=did).one().id


def _rule(app, pk, ntype, enabled=True, param=None, channels=None):
    with app.app_context():
        from app.models import User
        uid = User.query.filter_by(username="admin").one().id
        r = NotificationRule.query.filter_by(device_fk=pk, user_fk=uid, type=ntype).first() or NotificationRule(device_fk=pk, user_fk=uid, type=ntype)
        r.enabled, r.param, r.channels = enabled, param, channels
        r.fired = {}
        db.session.add(r)
        db.session.commit()


def _msgs(app, ntype=None):
    with app.app_context():
        q = Notification.query
        if ntype:
            q = q.filter_by(type=ntype)
        return [n.message for n in q.order_by(Notification.id)]


def test_page_groups_and_hidden_rows(app, admin, mc):
    html = admin.get(f"/meters/{mc}/notifications").get_data(as_text=True)
    for text in ("Limits", "Device Info", "Device Status", "Session renewed", "Session ended", "Exceeded kWh limit", "Max amp (trip)",
                 "Voltage out of range", "Sensor error", "No voltage", "Change of device status", "Change of a channel", "Device reconnected"):
        assert text in html, text
    # no tamper / UPS / battery / temperature on this meter: those rows are not offered
    for ntype in ("panel_open", "ups", "battery_low", "temp_high"):
        assert f'name="notify_{ntype}"' not in html
    # independent: channel boxes on the limit rows
    assert 'name="notify_session_end_ch"' in html and 'name="notify_kwh_remaining_ch"' in html and 'name="notify_voltage_range_ch"' not in html
    with app.app_context():
        d = db.session.get(Device, mc)
        d.caps = dict(d.caps, tamper=True, ups=True, battery=True, temp=1)
        db.session.commit()
    html = admin.get(f"/meters/{mc}/notifications").get_data(as_text=True)
    for ntype in ("panel_open", "ups", "battery_low", "temp_high"):
        assert f'name="notify_{ntype}"' in html


def test_save_params_and_channels(app, admin, mc):
    with app.app_context():
        d = db.session.get(Device, mc)
        d.caps = dict(d.caps, battery=True, temp=1)
        db.session.commit()
    data = {"notify_kwh_remaining": "on", "notify_kwh_remaining_param": "15", "notify_kwh_remaining_ch": ["1"],
            "notify_time_remaining": "on", "notify_time_remaining_param": "1:30",
            "notify_battery_low": "on", "notify_battery_low_param": "25", "notify_temp_high": "on", "notify_temp_high_param": "45",
            "notify_disconnected": "on", "notify_disconnected_param": "7"}
    admin.post(f"/meters/{mc}/notifications", data=data)
    with app.app_context():
        r = {x.type: x for x in NotificationRule.query.filter_by(device_fk=mc).all()}
        assert r["kwh_remaining"].param == 15 and r["kwh_remaining"].channels == [1]
        assert r["time_remaining"].param == 90 and r["time_remaining"].channels == []   # none picked
        assert r["battery_low"].param == 25 and r["temp_high"].param == 45 and r["disconnected"].param == 7
    # all channels picked = no limit
    admin.post(f"/meters/{mc}/notifications", data={"notify_kwh_remaining": "on", "notify_kwh_remaining_param": "15",
                                                    "notify_kwh_remaining_ch": ["0", "1"]})
    with app.app_context():
        assert NotificationRule.query.filter_by(device_fk=mc, type="kwh_remaining").one().channels is None
    # wrong values are refused and nothing is changed
    r = admin.post(f"/meters/{mc}/notifications", data={"notify_kwh_remaining": "on", "notify_kwh_remaining_param": "150"}, follow_redirects=True)
    assert b"Remaining % of the session kWh" in r.data and b"enter a value from 1 to 99" in r.data
    r = admin.post(f"/meters/{mc}/notifications", data={"notify_time_remaining": "on", "notify_time_remaining_param": "abc"}, follow_redirects=True)
    assert b"hh:mm" in r.data


def _ev(app, pk, seq, e, ch=None, **kw):
    with app.app_context():
        d = db.session.get(Device, pk)
        msg = {"t": "evt", "seq": seq, "ts": 1790000000 + seq, "tv": 1, "e": e, **kw}
        if ch is not None:
            msg["ch"] = ch
        ingest.handle(FakeConn(), d, msg)
        db.session.commit()


def test_channel_filter_and_session_end(app, admin, mc):
    _rule(app, mc, "session_end", channels=[1])
    _ev(app, mc, 1, "session_start", ch=0, sid=1, auto=False, max=0)
    _ev(app, mc, 2, "session_start", ch=1, sid=2, auto=False, max=0)
    _ev(app, mc, 3, "session_end", ch=0, sid=1, reason="expired", kwh=1.5)    # channel 0: not selected
    _ev(app, mc, 4, "session_end", ch=1, sid=2, reason="quota", kwh=2.0)      # channel 1: selected
    _ev(app, mc, 5, "session_end", ch=1, sid=3, reason="manual", kwh=0.1)     # a renew: has its own notification
    msgs = _msgs(app, "session_end")
    assert len(msgs) == 1 and "Session ended (kWh limit reached): 2.0 kWh" in msgs[0] and "CH2" in msgs[0]


def test_thresholds(app, admin, mc, monkeypatch):
    monkeypatch.setattr(hub, "is_online", lambda pk: True)
    _rule(app, mc, "kwh_remaining", param=20)
    _rule(app, mc, "time_remaining", param=30)
    _rule(app, mc, "battery_low", param=20)
    _rule(app, mc, "temp_high", param=40)
    t = now()

    def state(kwh0, kwh1, nr, bat, temp, sid1=7):
        with app.app_context():
            d = db.session.get(Device, mc)
            d.last_state = {"at": now(), "mode": "independent", "bat": bat,
                            "sessions": [{"ch": 0, "sid": 5, "status": "active", "kwh": kwh0, "max": 10, "next_reset": nr},
                                         {"ch": 1, "sid": sid1, "status": "active", "kwh": kwh1, "max": 10}]}
            d.last_reading = {"t": [temp]}
            db.session.commit()
            notify_limits.run()

    state(5.0, 1.0, t + 7200, 80, 20)                      # nothing yet
    assert _msgs(app) == []
    state(8.5, 1.0, t + 1200, 15, 41)                      # CH1 85 % used (15 % left), 20 min left, battery 15, 41 degC
    msgs = _msgs(app)
    assert any("CH1" in m and "15% of the session kWh" in m for m in msgs)
    assert any("(h:mm)" in m and "0:20" in m for m in msgs)
    assert any("Battery is at 15%" in m for m in msgs) and any("41" in m and "above 40" in m for m in msgs)
    n = len(msgs)
    state(9.0, 1.0, t + 1100, 14, 42)                      # still low: nothing more
    assert len(_msgs(app)) == n
    state(2.0, 9.5, t + 9000, 40, 30, sid1=8)              # recovered; CH2 of a new session is now low
    assert any("CH2" in m and "kWh is left" in m for m in _msgs(app)[n:])
    n = len(_msgs(app))
    state(8.9, 1.0, t + 900, 12, 45, sid1=8)               # armed again: CH1 (new numbers), battery, temp, time
    assert len(_msgs(app)) > n


def test_channel_status_message(app, admin, mc):
    admin.post(f"/meters/{mc}/configure", data=_form(mode="independent", ch_off_1="on"))
    admin.post(f"/meters/{mc}/configure", data=_form(mode="independent"))
    msgs = _msgs(app, "channel_status")
    assert any("CH2" in m and "disabled by admin" in m for m in msgs) and any("enabled by admin" in m for m in msgs)
