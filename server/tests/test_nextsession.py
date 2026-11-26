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
