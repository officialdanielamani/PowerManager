"""Notifications: in-app always, Telegram optional. A cooldown stops repeated spam."""
import json
import logging
import threading
import urllib.parse
import urllib.request

from ..extensions import db
from ..models import Notification, NotificationRule, now
from . import settings

log = logging.getLogger(__name__)
COOLDOWN_S = 600
# Only types that can repeat quickly get a cooldown; the others are naturally rare.
COOLDOWN_TYPES = {"max_amp", "panel_open", "no_voltage"}


def rule(device_pk, ntype):
    return db.session.get(NotificationRule, (device_pk, ntype))


def notify(device, ntype, message, force=False):
    """Create a notification if the device's rule for `ntype` is enabled."""
    r = rule(device.id, ntype)
    if not force and (r is None or not r.enabled):
        return None
    if ntype in COOLDOWN_TYPES:
        last = (Notification.query.filter_by(device_fk=device.id, type=ntype)
                .order_by(Notification.ts.desc()).first())
        if last is not None and now() - last.ts < COOLDOWN_S:
            return None
    text = f"[{device.name}] {message}"
    n = Notification(device_fk=device.id, type=ntype, message=text[:300])
    if settings.get("telegram_enabled") == "1":
        token, chat = settings.get("telegram_bot_token"), settings.get("telegram_chat_id")
        threading.Thread(target=_telegram_bg, args=(token, chat, text), daemon=True).start()
    db.session.add(n)
    return n


def send_telegram(token, chat_id, text):
    """Send one message; returns (ok, error)."""
    if not token or not chat_id:
        return False, "Bot token and chat ID are required"
    url = f"https://api.telegram.org/bot{urllib.parse.quote(token, safe=':')}/sendMessage"
    data = urllib.parse.urlencode({"chat_id": chat_id, "text": text}).encode()
    try:
        with urllib.request.urlopen(url, data=data, timeout=10) as resp:
            body = json.loads(resp.read().decode())
            return bool(body.get("ok")), body.get("description")
    except Exception as exc:  # network errors, HTTP errors
        return False, str(exc)


def _telegram_bg(token, chat_id, text):
    ok, err = send_telegram(token, chat_id, text)
    if not ok:
        log.warning("Telegram send failed: %s", err)
