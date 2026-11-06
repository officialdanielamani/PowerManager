"""Commands from the server to a meter, queued while it is offline (docs/protocol.md §2.3)."""
from ..extensions import db
from ..models import Command, now
from . import hub

KNOWN = {"relay", "renew", "end", "send_now", "reset_trip", "get_state", "remove", "disable", "enable", "get_info"}
QUEUE_MAX_AGE_S = 7 * 86400


def session_state(device, ch=None):
    """The meter's session for channel `ch` (multi-channel independent mode), else the meter's session."""
    st = device.last_state or {}
    if ch is not None and device.mc_mode == "independent":
        for s in st.get("sessions") or []:
            if s.get("ch") == ch:
                return s
        return {}
    return st.get("sess") or {}


def relay_block_reason(device, ch=None):
    """Why relay ON is not allowed right now (server-side check), or None."""
    if device.disabled_at:
        return "disabled"
    st = device.last_state or {}
    if ch is not None and device.is_mc:
        relays = [r for r in st.get("relay") or [] if r.get("i") == ch]
        if relays and relays[0].get("tripped"):
            return "tripped"
    elif st.get("tripped"):
        return "tripped"
    status = session_state(device, ch).get("status")
    if status == "quota":
        return "quota_exceeded"
    if status in ("expired", "ended"):
        return "session_expired"
    return None


def _message(cmd):
    msg = {"t": "cmd", "c": cmd.c, "ref": cmd.id}
    msg.update(cmd.params or {})
    return msg


def issue(device, c, params=None, user=None):
    """Create a command and send it if the device is connected.

    Returns (command, error). error is a short code for the UI."""
    if c not in KNOWN:
        return None, "unknown_cmd"
    params = params or {}
    if device.disabled_at and c in ("renew", "end", "relay", "reset_trip"):
        if c != "relay" or params.get("on"):
            return None, "disabled"
    if c == "relay" and params.get("on"):
        reason = relay_block_reason(device, params.get("ch"))
        if reason:
            return None, reason
    online = hub.is_online(device.id)
    if c == "send_now" and not online:
        return None, "offline"
    if c == "relay":
        # Only the latest relay command matters.
        for old in Command.query.filter_by(device_fk=device.id, c="relay", sent_at=None, cancelled=False).all():
            if (old.params or {}).get("ch") == params.get("ch"):  # per channel on multi-channel meters
                old.cancelled = True
    cmd = Command(device_fk=device.id, c=c, params=params,
                  created_by=user.id if user else None)
    db.session.add(cmd)
    db.session.flush()
    if online and hub.send(device.id, _message(cmd)) is not None:
        cmd.sent_at = now()
    db.session.commit()
    return cmd, None


def flush_pending(device, conn):
    """Send queued commands after (re)connect."""
    pending = (Command.query
               .filter(Command.device_fk == device.id, Command.sent_at.is_(None),
                       Command.cancelled.is_(False),
                       Command.created_at >= now() - QUEUE_MAX_AGE_S)
               .order_by(Command.id).all())
    for cmd in pending:
        if cmd.c == "send_now":
            cmd.cancelled = True
            continue
        conn.send(_message(cmd))
        cmd.sent_at = now()


def ack(device, ref, ok, err=None):
    cmd = db.session.get(Command, ref) if isinstance(ref, int) else None
    if cmd is None or cmd.device_fk != device.id:
        return None
    cmd.acked_at = now()
    cmd.ok = bool(ok)
    cmd.err = (err or None) and str(err)[:40]
    return cmd
