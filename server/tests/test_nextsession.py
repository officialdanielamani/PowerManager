"""Schedule Next Session (per channel, independent mode) and the voltage default button."""
import pytest

from app.extensions import db
from app.models import Command, Device, Event, Notification, now
from app.services import hub, nextsession

from .test_multichannel import _form, _pair_mc


@pytest.fixture()
def mc(app, admin):
    from .test_pairing import _code
    assert _pair_mc(admin, _code(admin), "NXT00001").status_code == 200
    with app.app_context():
        pk = Device.query.filter_by(device_id="NXT00001").one().id
    admin.post(f"/meters/{pk}/configure", data=_form(mode="independent", amp_max_0="12", kwh_max_0="30", kwh_action_0="end",
                                                     sess_mode_0="daily", daily_at_0="22:00", auto_0="on"))
    return pk


@pytest.fixture()
def online(monkeypatch):
    sent = []
    monkeypatch.setattr(hub, "is_online", lambda pk: True)
    monkeypatch.setattr(hub, "send", lambda pk, msg: sent.append(msg) or 1)
    return sent


BODY = {"action": "set", "ch": 0, "when": "rel", "d": 0, "h": 1, "m": 0, "amp_max": "13.5", "kwh_max": "30", "kwh_action": "end",
        "sess_mode": "daily", "daily_at": "23:30", "auto": "on"}


def _next(app, pk, ch=0):
    with app.app_context():
        return (db.session.get(Device, pk).cfg["ch"][ch] or {}).get("next")


def test_set_validate_clear(app, admin, mc):
    r = admin.post(f"/meters/{mc}/next-session", json=BODY).get_json()
    assert r["ok"] and r["next"]["amp_max"] == 13.5 and 3500 < r["next"]["at"] - now() <= 3601 and r["next"]["at_local"]
    n = _next(app, mc)
    assert n["daily_at"] == "23:30" and n["auto"] is True and n["by"] == "admin"
    # the current settings are untouched, and a normal Save of the page keeps the schedule
    with app.app_context():
        assert db.session.get(Device, mc).cfg["ch"][0]["amp_max"] == 12
    admin.post(f"/meters/{mc}/configure", data=_form(mode="independent", amp_max_0="12", kwh_max_0="30", kwh_action_0="end",
                                                     sess_mode_0="daily", daily_at_0="22:00", auto_0="on", name_0="Kitchen"))
    assert _next(app, mc)["amp_max"] == 13.5
    # the Info page data says when
    chs = admin.get("/api/ui/meters").get_json()["meters"][0]["channels"]
    assert chs[0]["next_at"] and chs[1]["next_at"] is None
    # at least 5 minutes ahead
    bad = admin.post(f"/meters/{mc}/next-session", json=dict(BODY, h=0, m=4))
    assert bad.status_code == 400 and "5 minutes" in " ".join(bad.get_json()["errors"])
    assert admin.post(f"/meters/{mc}/next-session", json=dict(BODY, when="cal", date="2001-01-01T10:00")).status_code == 400
    assert admin.post(f"/meters/{mc}/next-session", json=dict(BODY, amp_max="9999")).status_code == 400
    assert admin.post(f"/meters/{mc}/next-session", json=dict(BODY, when="cal", date="")).status_code == 400
    # a calendar time in the future works
    from datetime import datetime, timedelta
    when = (datetime.utcnow() + timedelta(days=3)).strftime("%Y-%m-%dT%H:%M")
    assert admin.post(f"/meters/{mc}/next-session", json=dict(BODY, when="cal", date=when)).get_json()["ok"]
    # OFF: removed
    assert admin.post(f"/meters/{mc}/next-session", json={"action": "clear", "ch": 0}).get_json()["cleared"]
    assert _next(app, mc) is None
    # shared mode: not offered
    admin.post(f"/meters/{mc}/configure", data=_form(mode="shared"))
    assert admin.post(f"/meters/{mc}/next-session", json=BODY).status_code == 400


def test_execute_with_and_without_auto_renew(app, admin, mc, online):
    admin.post(f"/meters/{mc}/next-session", json=BODY)
    admin.post(f"/meters/{mc}/next-session", json=dict(BODY, ch=1, auto="", amp_max="7", sess_mode="none"))
    with app.app_context():                                   # make both due
        d = db.session.get(Device, mc)
        import copy
        cfg = copy.deepcopy(d.cfg)
        for c in cfg["ch"]:
            c["next"]["at"] = now() - 5
        d.cfg = cfg
        d.cfg_ver_wanted = 5
        db.session.commit()
        nextsession.run()
        d = db.session.get(Device, mc)
        assert d.cfg["ch"][0]["amp_max"] == 13.5 and d.cfg["ch"][0]["daily_at"] == "23:30" and "next" not in d.cfg["ch"][0]
        assert d.cfg["ch"][1]["amp_max"] == 7 and d.cfg["ch"][1]["sess_mode"] == "none" and "next" not in d.cfg["ch"][1]
        assert d.cfg_ver_wanted == 7                                       # one new config per channel
        cmds = [(c.c, c.params) for c in Command.query.filter_by(device_fk=mc).order_by(Command.id)]
        assert ("renew", {"ch": 0}) in cmds and ("end", {"ch": 1}) in cmds   # auto renew starts the new session; otherwise it just ends
        assert any(m.get("t") == "cfg" for m in online)
        assert Event.query.filter_by(device_fk=mc, type="next_session").count() == 2
        assert any("scheduled session settings" in n.message for n in Notification.query.all())
        nextsession.run()                                                  # nothing left to do
        assert Command.query.filter_by(device_fk=mc).count() == len(cmds)


def test_voltage_default_button(app, admin, mc):
    html = admin.get(f"/meters/{mc}/configure").get_data(as_text=True)
    assert 'id="voltDefault"' in html and "Set to Default" in html and "216.2" in html
    assert "Schedule Next Session" in html and "This will be set on" in html


# ---- single channel / Shared mode, weekly sessions, the meter carries the schedule out itself

def _single(app, admin):
    from .test_pairing import _code, _pair
    assert _pair(admin, _code(admin), "NXT00002").status_code == 200
    with app.app_context():
        return Device.query.filter_by(device_id="NXT00002").one().id


def test_weekly_session_config(app, admin, mc):
    from app.services import devconfig, settings
    days = dict(wk_mon="on", wk_mon_at="08:00", wk_wed="on", wk_wed_at="18:30", wk_sun="on", wk_sun_at="00:15")
    r = admin.post(f"/meters/{mc}/configure", data=_form(mode="shared", sess_mode="weekly", auto="on", **days))
    assert r.status_code in (200, 302)
    with app.app_context():
        d = db.session.get(Device, mc)
        assert d.cfg["week"] == {"mon": "08:00", "wed": "18:30", "sun": "00:15"}
        doc = devconfig.to_device(d.cfg, 3)["cfg"]["sess"]
        assert doc["mode"] == "weekly" and doc["wk"] == sorted([settings.local_week_to_utc(0, "08:00"),
                                                              settings.local_week_to_utc(2, "18:30"), settings.local_week_to_utc(6, "00:15")])
        assert devconfig.describe_session(d.cfg) == "Mon 08:00, Wed 18:30, Sun 00:15"
        _, errors = devconfig.parse_form(_form(sess_mode="weekly"), d)
        assert any("at least one day" in e for e in errors)
    # all days, different times; per channel in Independent mode
    allw = {f"wk_{x}_1": "on" for x in devconfig.WEEKDAYS} | {f"wk_{x}_at_1": f"0{i}:00" for i, x in enumerate(devconfig.WEEKDAYS)}
    admin.post(f"/meters/{mc}/configure", data=_form(mode="independent", sess_mode_1="weekly", **allw))
    with app.app_context():
        d = db.session.get(Device, mc)
        assert len(d.cfg["ch"][1]["week"]) == 7 and d.cfg["ch"][1]["week"]["thu"] == "03:00"
        assert len(devconfig.to_device(d.cfg, 1)["cfg"]["ch"][1]["sess"]["wk"]) == 7


def test_schedule_single_channel_meter(app, admin, online):
    pk = _single(app, admin)
    body = {"action": "set", "when": "rel", "d": 0, "h": 1, "m": 0, "amp_max": "9", "kwh_max": "5", "kwh_action": "end",
            "sess_mode": "weekly", "week": {"fri": "20:00", "sat": "09:00"}, "auto": "on"}
    r = admin.post(f"/meters/{pk}/next-session", json=body).get_json()
    assert r["ok"]
    with app.app_context():
        from app.services import devconfig
        d = db.session.get(Device, pk)
        assert d.cfg["next"]["amp_max"] == 9 and d.cfg["next"]["week"] == {"fri": "20:00", "sat": "09:00"}
        doc = devconfig.to_device(d.cfg, 4)["cfg"]["next"]
        assert doc["at"] == d.cfg["next"]["at"] and doc["amp_max"] == 9 and doc["kwh_max"] == 5 and doc["sess"]["mode"] == "weekly"
        assert len(doc["sess"]["wk"]) == 2
    assert any(m.get("t") == "cfg" and m["cfg"].get("next") for m in online)      # the meter holds it
    # a weekly schedule needs a day
    assert admin.post(f"/meters/{pk}/next-session", json=dict(body, week={})).status_code == 400
    # the meter ran it while offline: its event reaches the server later and the server follows
    with app.app_context():
        d = db.session.get(Device, pk)
        at = d.cfg["next"]["at"]
        nextsession.device_applied(d, None, at)
        d = db.session.get(Device, pk)
        assert "next" not in d.cfg and d.cfg["ch"][0]["amp_max"] == 9 and d.cfg["ch"][0]["kwh_max"] == 5 and d.cfg["sess_mode"] == "weekly"
        assert Command.query.filter_by(device_fk=pk).count() == 0               # nothing to send: the meter already does it
    # the schedule was cleared on the server while the meter was offline: the meter is put back to the server's settings
    admin.post(f"/meters/{pk}/next-session", json=body)
    admin.post(f"/meters/{pk}/next-session", json={"action": "clear"})
    online.clear()
    with app.app_context():
        nextsession.device_applied(db.session.get(Device, pk), None, 12345)
        assert any(m.get("t") == "cfg" and "next" not in m["cfg"] for m in online)


def test_schedule_shared_mode_and_server_fallback(app, admin, mc, online):
    admin.post(f"/meters/{mc}/configure", data=_form(mode="shared", kwh_max="10", kwh_action="hold", sess_mode="daily", daily_at="06:00"))
    body = {"action": "set", "when": "rel", "d": 0, "h": 2, "m": 0, "kwh_max": "20", "kwh_action": "end", "sess_mode": "none", "auto": "on"}
    assert "amp_max" not in admin.post(f"/meters/{mc}/next-session", json=body).get_json()["next"]
    assert admin.post(f"/meters/{mc}/next-session", json=dict(body, ch=0)).status_code == 400      # a channel number means nothing here
    with app.app_context():
        d = db.session.get(Device, mc)
        assert d.cfg["next"]["kwh_max"] == 20 and (d.cfg["ch"][0].get("next") is None)
        import copy
        cfg = copy.deepcopy(d.cfg)
        cfg["next"]["at"] = now() - 5
        d.cfg = cfg
        db.session.commit()
        # the meter reports the capability: it carries the schedule out itself, the server leaves it alone
        d.caps = dict(d.caps or {}, next=1)
        db.session.commit()
        nextsession.run()
        assert "next" in db.session.get(Device, mc).cfg
        d.caps = dict(d.caps, next=0)
        db.session.commit()
        nextsession.run()                                  # old firmware: the server does it
        d = db.session.get(Device, mc)
        assert "next" not in d.cfg and d.cfg["kwh_max"] == 20 and d.cfg["sess_mode"] == "none"
        assert ("renew", {}) in [(c.c, c.params) for c in Command.query.filter_by(device_fk=mc)]


def test_config_page_shows_weekly_and_meter_panel(app, admin, mc):
    html = admin.get(f"/meters/{mc}/configure").get_data(as_text=True)
    assert "Days of the week" in html and "pm-wk-day" in html and 'name="wk_mon_at_1"' in html
    assert html.count("Schedule Next Session") >= 3          # two channels + the whole meter (Shared mode)
