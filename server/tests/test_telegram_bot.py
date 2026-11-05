"""Interactive Telegram bot: replies are built by handle_update() (no network here)."""
import pytest

from app.extensions import db
from app.models import Device, MeterViewer, User, now
from app.routes.device_api import ensure_device_defaults
from app.services import telegram_bot as bot


@pytest.fixture()
def setup(app):
    with app.app_context(), app.test_request_context():
        adm = User(username="adm", role="admin", telegram_chat_id="111")
        adm.set_password("Str0ng!Passw0rd")
        viewer = User(username="vee", role="viewer", telegram_chat_id="222")
        viewer.set_password("Str0ng!Passw0rd")
        db.session.add_all([adm, viewer])
        meters = []
        for did, name, kind in (("ACM00001", "Dryer", "ac"), ("DCM00002", "Solar", "dc")):
            d = Device(device_id=did, name=name, caps={"channels": 1, "kind": kind}, cfg={}, pair_gen=1)
            db.session.add(d)
            db.session.flush()
            ensure_device_defaults(d, reset=True)
            meters.append(d)
        meters[1].last_reading = {"ts": now() - 30, "ch": [{"i": 0, "v": 48.12, "a": 3.5, "w": 168.4, "e": 2.0}]}
        db.session.add(MeterViewer(device_fk=meters[1].id, user_fk=viewer.id))  # viewer: Solar only
        db.session.commit()
        yield {"ac": meters[0].id, "dc": meters[1].id}


def msg(chat, text):
    return {"update_id": 1, "message": {"chat": {"id": chat}, "text": text}}


def texts(replies):
    return [p.get("text", "") for m, p in replies if m == "sendMessage"]


def test_unknown_chat_gets_its_id(app, setup):
    with app.app_context(), app.test_request_context():
        t = texts(bot.handle_update(msg(999, "/start")))
    assert "999" in t[0] and "not linked" in t[0]


def test_start_shows_menu_and_list(app, setup):
    with app.app_context(), app.test_request_context():
        r = bot.handle_update(msg(111, "/start"))
        assert r[0][1]["reply_markup"]["keyboard"][0][0]["text"] == bot.BTN_LIST
        t = texts(bot.handle_update(msg(111, bot.BTN_LIST)))[0]
    assert "ACM00001 · Dryer · AC" in t and "DCM00002 · Solar · DC" in t


def test_viewer_sees_only_linked_meters(app, setup):
    with app.app_context(), app.test_request_context():
        t = texts(bot.handle_update(msg(222, "/list")))[0]
        assert "Solar" in t and "Dryer" not in t
        r = bot.handle_update({"update_id": 2, "callback_query": {"id": "q", "data": f"now:{setup['ac']}",
                                                                "message": {"chat": {"id": 222}}}})
        assert "not available" in texts(r)[0]


def test_now_reading_by_name_and_button(app, setup):
    with app.app_context(), app.test_request_context():
        t = texts(bot.handle_update(msg(111, "/now solar"), wait_s=0))[0]
        assert "Solar (DCM00002) · DC" in t and "48.12 V" in t and "PF" not in t and "offline" in t
        r = bot.handle_update(msg(111, bot.BTN_NOW))
        buttons = r[0][1]["reply_markup"]["inline_keyboard"]
        assert any(b["callback_data"] == f"now:{setup['dc']}" for row in buttons for b in row)
        r = bot.handle_update({"update_id": 3, "callback_query": {"id": "q", "data": f"now:{setup['dc']}",
                                                                "message": {"chat": {"id": 111}}}}, wait_s=0)
        assert r[0][0] == "answerCallbackQuery" and "168.4 W" in texts(r)[0]
