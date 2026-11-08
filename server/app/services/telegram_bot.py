"""Interactive Telegram bot: List meters, Now Reading (Get Data Now) for a chosen meter.

Uses long polling (getUpdates), so it works behind a Cloudflare Tunnel or NAT without a webhook.
Only chats whose ID is saved in a PowerManager account (Settings -> Account -> My Telegram) get
answers, and each account sees only the meters it may view (admin: all; viewer: linked meters).

    /start, /menu   keyboard with [List] [Now Reading]
    List            every meter: Device ID, nick name, online, power now
    Now Reading     pick a meter (buttons), or "/now <Device ID or nick name>"
"""
import json
import logging
import threading
import time
import urllib.parse
import urllib.request

from ..extensions import db
from ..models import Device, User, now
from . import access, commands, hub, settings

log = logging.getLogger(__name__)

BTN_LIST = "📋 List"
BTN_NOW = "⚡ Now Reading"
WAIT_READING_S = 12      # how long "Now Reading" waits for the meter's fresh reading
POLL_TIMEOUT_S = 25      # long-poll timeout of getUpdates
MAX_BUTTONS = 40

_thread = None


# ------------------------------------------------------------------ Telegram API

def api(token, method, payload, timeout=POLL_TIMEOUT_S + 10):
    url = f"https://api.telegram.org/bot{urllib.parse.quote(token, safe=':')}/{method}"
    req = urllib.request.Request(url, data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


# ------------------------------------------------------------------ replies (pure, testable)

def _menu_keyboard():
    return {"keyboard": [[{"text": BTN_LIST}, {"text": BTN_NOW}]], "resize_keyboard": True, "is_persistent": True}


def _user_for_chat(chat_id):
    u = User.query.filter_by(telegram_chat_id=str(chat_id)).first()
    return u if u is not None and u.is_enabled else None


def _meters(user):
    return access.visible_devices(user).all()


def _find(user, query):
    q = (query or "").strip().lower()
    if not q:
        return None
    meters = _meters(user)
    for d in meters:
        if d.device_id.lower() == q or d.name.lower() == q:
            return d
    hits = [d for d in meters if q in d.name.lower() or q in d.device_id.lower()]
    return hits[0] if len(hits) == 1 else None


def _fmt(v, nd):
    return "–" if v is None else f"{float(v):.{nd}f}"


def reading_text(device, user):
    """The latest reading of a meter as a Telegram message."""
    from ..routes.main import meter_summary
    m = meter_summary(device, show_price=access.can_price(user), user=user)
    lines = [f"⚡ {device.name} ({device.device_id}) · {device.kind.upper()}",
             f"{'🟢 online' if m['online'] else '🔴 offline'} · relay {'ON' if m['relay_on'] else 'OFF'}"]
    if m.get("reading_err"):
        lines.append({"no_reply": "⚠️ Sensor not responding", "bad": "⚠️ Sensor error",
                      "no_ac": "⚠️ No " + ("DC supply" if device.kind == "dc" else "AC power")}
                     .get(m["reading_err"], "⚠️ No reading"))
    if m.get("mc"):  # multi-channel: one line per channel
        for c in m["channels"]:
            line = (f"{c['name']}: {_fmt(c.get('w'), 1)} W · {_fmt(c.get('a'), 3)} A · "
                    f"{'ON' if c['relay_on'] else ('TRIP' if c['tripped'] else 'OFF')}")
            if device.mc_mode == "independent" and c.get("kwh") is not None:
                line += f" · {_fmt(c['kwh'], 3)}" + (f"/{c['kwh_max']:g}" if c.get("kwh_max") else "") + " kWh"
                if c.get("cost"):
                    line += f" · {c['cost']}"
            lines.append(line)
        volts = next((x.get("v") for x in m.get("reading") or [] if x.get("v") is not None), None)
        if volts is not None:
            lines.append(f"Voltage {_fmt(volts, 1)} V")
    for c in [] if m.get("mc") else (m.get("reading") or []):
        dc = device.kind == "dc"
        lines.append(f"Voltage {_fmt(c.get('v'), 2 if dc else 1)} V · Current {_fmt(c.get('a'), 2 if dc else 3)} A")
        lines.append(f"Power {_fmt(c.get('w'), 1)} W" + ("" if dc else f" · PF {_fmt(c.get('pf'), 2)} · {_fmt(c.get('hz'), 1)} Hz"))
        lines.append(f"Energy total {_fmt(c.get('e'), 3)} kWh")
    if m.get("kwh") is not None and not (m.get("mc") and device.mc_mode == "independent"):
        limit = f" / {m['kwh_max']:g} kWh" if m.get("kwh_max") else ""
        lines.append(f"Session used {_fmt(m['kwh'], 3)} kWh{limit}" + (f" · {m['cost']}" if m.get("cost") else ""))
    if m.get("temps"):
        lines.append(("🔥 " if m.get("temp_high") else "🌡 ") + " · ".join(
            f"{t['name']} {_fmt(t['c'], 1)} °C" + (f" {_fmt(t['rh'], 0)} %RH" if t.get("rh") is not None else "")
            for t in m["temps"]))
    lines.append(f"Reading at {m.get('reading_at') or '–'} ({settings.get('timezone')})")
    return "\n".join(lines)


def _now_reading(device, user, wait_s):
    """Get Data Now, wait a little for the fresh reading, then the latest reading."""
    note = ""
    if hub.is_online(device.id):
        before = (device.last_reading or {}).get("ts", 0)
        _cmd, err = commands.issue(device, "send_now", {}, user)
        db.session.commit()
        if not err:
            deadline = time.time() + wait_s
            while time.time() < deadline:
                time.sleep(1)
                db.session.refresh(device)
                if (device.last_reading or {}).get("ts", 0) > before:
                    break
            else:
                note = "\n(The meter did not answer in time: this is the latest reading the server has.)"
    else:
        note = "\n(The meter is offline: this is the last reading the server has.)"
    return reading_text(device, user) + note


def handle_update(upd, wait_s=WAIT_READING_S):
    """Return a list of (method, payload) replies for one Telegram update."""
    out = []
    if "callback_query" in upd:
        cq = upd["callback_query"]
        chat_id = (cq.get("message") or {}).get("chat", {}).get("id")
        out.append(("answerCallbackQuery", {"callback_query_id": cq.get("id")}))
        user = _user_for_chat(chat_id)
        data = str(cq.get("data") or "")
        if user is None or not data.startswith("now:") or not data[4:].isdigit():
            return out
        device = db.session.get(Device, int(data[4:]))
        if device is None or device.purged or device.removed_at or not access.can_view(user, device):
            out.append(("sendMessage", {"chat_id": chat_id, "text": "That meter is not available."}))
            return out
        out.append(("sendMessage", {"chat_id": chat_id, "text": _now_reading(device, user, wait_s)}))
        return out

    msg = upd.get("message") or {}
    chat_id = (msg.get("chat") or {}).get("id")
    text = (msg.get("text") or "").strip()
    if chat_id is None or not text:
        return out
    user = _user_for_chat(chat_id)
    if user is None:
        out.append(("sendMessage", {"chat_id": chat_id, "text":
                    f"This chat is not linked to a PowerManager account.\nYour chat ID is {chat_id}: "
                    "add it in PowerManager → Settings → Account → My Telegram, then send /start again."}))
        return out

    if text in (BTN_LIST, BTN_NOW):  # keyboard buttons (their labels contain spaces)
        cmd, arg = ("/list" if text == BTN_LIST else "/now"), ""
    else:
        cmd, _, arg = text.partition(" ")
        cmd = cmd.split("@")[0].lower()  # "/list@MyBot" in groups
    if cmd in ("/start", "/menu", "/help"):
        out.append(("sendMessage", {"chat_id": chat_id, "reply_markup": _menu_keyboard(), "text":
                    f"Hi {user.username}! PowerManager bot.\n"
                    f"{BTN_LIST}: all your meters\n{BTN_NOW}: latest reading of a meter (asks the meter now)\n"
                    "Or type /now <Device ID or nick name>."}))
    elif text == BTN_LIST or cmd == "/list":
        meters = _meters(user)
        if not meters:
            out.append(("sendMessage", {"chat_id": chat_id, "text": "No meters for your account yet."}))
        else:
            from ..routes.main import meter_summary
            rows = []
            for d in meters:
                m = meter_summary(d, show_price=False, user=user)
                w = f"{m['w']:g} W" if m.get("w") is not None else "–"
                rows.append(f"{'🟢' if m['online'] else '🔴'} {d.device_id} · {d.name} · {d.kind.upper()} · {w}")
            out.append(("sendMessage", {"chat_id": chat_id, "text": f"Your meters ({len(meters)}):\n" + "\n".join(rows)}))
    elif text == BTN_NOW or cmd == "/now":
        if arg.strip():
            device = _find(user, arg)
            if device is None:
                out.append(("sendMessage", {"chat_id": chat_id, "text": f"No single meter matches '{arg.strip()}'. Send {BTN_LIST} to see them."}))
            else:
                out.append(("sendMessage", {"chat_id": chat_id, "text": _now_reading(device, user, wait_s)}))
        else:
            meters = _meters(user)[:MAX_BUTTONS]
            if not meters:
                out.append(("sendMessage", {"chat_id": chat_id, "text": "No meters for your account yet."}))
            else:
                buttons = [{"text": f"{d.name} ({d.device_id})", "callback_data": f"now:{d.id}"} for d in meters]
                rows = [buttons[i:i + 2] for i in range(0, len(buttons), 2)]
                out.append(("sendMessage", {"chat_id": chat_id, "text": "Which meter?",
                                            "reply_markup": {"inline_keyboard": rows}}))
    else:
        out.append(("sendMessage", {"chat_id": chat_id, "reply_markup": _menu_keyboard(),
                                    "text": f"Use the buttons: {BTN_LIST} or {BTN_NOW}."}))
    return out


# ------------------------------------------------------------------ polling thread

def _run(app):
    offset, token_used = None, None
    while True:
        with app.app_context():
            try:
                token = settings.get("telegram_bot_token") if settings.get("telegram_enabled") == "1" else ""
            finally:
                db.session.remove()
        if not token:
            time.sleep(15)
            continue
        try:
            if token != token_used:  # new / changed bot: skip messages sent while we were not running
                last = api(token, "getUpdates", {"offset": -1, "timeout": 0}).get("result") or []
                offset = last[-1]["update_id"] + 1 if last else None
                token_used = token
            payload = {"timeout": POLL_TIMEOUT_S, "allowed_updates": ["message", "callback_query"]}
            if offset is not None:
                payload["offset"] = offset
            updates = api(token, "getUpdates", payload).get("result") or []
        except Exception as exc:  # network down, bad token, webhook set, ...
            log.warning("Telegram bot: %s", exc)
            time.sleep(30)
            continue
        for upd in updates:
            offset = upd["update_id"] + 1
            # One short thread per update: a "Now Reading" that waits for a meter does not hold up others.
            threading.Thread(target=_answer, args=(app, token, upd), daemon=True).start()


def _answer(app, token, upd):
    with app.app_context(), app.test_request_context():
        try:
            replies = handle_update(upd)
            db.session.commit()
        except Exception:
            log.exception("Telegram bot: update failed")
            db.session.rollback()
            replies = []
        finally:
            db.session.remove()
    for method, payload in replies:
        try:
            api(token, method, payload, timeout=15)
        except Exception as exc:
            log.warning("Telegram bot: %s failed: %s", method, exc)


def start(app):
    """Start the bot thread once (it idles while Telegram is off or has no token)."""
    global _thread
    if _thread is None:
        _thread = threading.Thread(target=_run, args=(app,), daemon=True, name="telegram-bot")
        _thread.start()
    return _thread
