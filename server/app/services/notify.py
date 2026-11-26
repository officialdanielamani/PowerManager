"""Notifications per meter AND per user: every account chooses its own (meters/<id>/notifications).

Each matching account gets its own in-app notification; Telegram goes to that account's chat ID
(the bot token is set once by the admin). A cooldown stops repeated spam per user.
"""
import json
import logging
import threading
import urllib.parse
import urllib.request

from ..extensions import db
from ..models import Notification, NotificationRule, User, now
from . import access, settings

log = logging.getLogger(__name__)
COOLDOWN_S = 600
# Only types that can repeat quickly get a cooldown; the others are naturally rare.
COOLDOWN_TYPES = {"max_amp", "panel_open", "no_voltage"}


def _wants_channel(device, rule, ch):
    """A rule limited to some channels (independent mode) only applies to those. No channel given / no limit = applies."""
    if rule is None or rule.channels is None or ch is None or not device.is_mc:
        return True
    return int(ch) in [int(c) for c in rule.channels]


def subscribers(device, ntype, ch=None):
    """(user, rule) pairs that want `ntype` (for channel `ch`) on this meter and may still see it. For default-on
    types (devconfig.NOTIFY_DEFAULT_ON) an account without a saved choice counts as subscribed."""
    from .devconfig import NOTIFY_DEFAULT_ON
    out = []
    if ntype in NOTIFY_DEFAULT_ON:
        rules = {r.user_fk: r for r in NotificationRule.query.filter_by(device_fk=device.id, type=ntype).all()}
        for user in User.query.order_by(User.id).all():
            r = rules.get(user.id)
            if (r is None or r.enabled) and user.is_enabled and access.can_view(user, device) and _wants_channel(device, r, ch):
                out.append((user, r))
        return out
    for r in NotificationRule.query.filter_by(device_fk=device.id, type=ntype, enabled=True).all():
        user = db.session.get(User, r.user_fk)
        if user is not None and user.is_enabled and access.can_view(user, device) and _wants_channel(device, r, ch):
            out.append((user, r))
    return out


def notify(device, ntype, message, only_user=None, subs=None, name=None, ch=None):
    """Create a notification for every account subscribed to `ntype` on this meter.
    `subs` / `name`: subscribers and meter name taken before the meter's data was removed.
    `ch`: the channel the message is about (users who limited the option to other channels are skipped)."""
    created = []
    text = f"[{name or device.name}] {message}"
    for user, _rule in (subs if subs is not None else subscribers(device, ntype, ch)):
        if only_user is not None and user.id != only_user.id:
            continue
        if ntype in COOLDOWN_TYPES:
            last = (Notification.query.filter_by(device_fk=device.id, user_fk=user.id, type=ntype)
                    .order_by(Notification.ts.desc()).first())
            if last is not None and now() - last.ts < COOLDOWN_S:
                continue
        n = Notification(device_fk=device.id, user_fk=user.id, type=ntype, message=text[:300])
        db.session.add(n)
        created.append(n)
        if settings.get("telegram_enabled") == "1" and user.telegram_chat_id:
            threading.Thread(target=_telegram_bg, daemon=True,
                             args=(settings.get("telegram_bot_token"), user.telegram_chat_id, text)).start()
    return created


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
