"""Meter-facing endpoints: pairing (HTTPS) and runtime WebSocket (docs/protocol.md)."""
import json
import logging
import re
import secrets
import time

from flask import Blueprint, jsonify, request
from simple_websocket import ConnectionClosed

from ..extensions import db, limiter, sock
from ..models import Channel, Device, NotificationRule, PairingCode, now
from ..services import devconfig, hub, ingest

bp = Blueprint("device_api", __name__)
log = logging.getLogger(__name__)

DEVICE_ID_RE = re.compile(r"^[A-Z0-9]{4,16}$")
OFFLINE_AFTER_S = 75
WS_PATH = "/ws/device"


def normalize_code(code):
    return re.sub(r"[^A-Z0-9]", "", (code or "").upper())


def ensure_device_defaults(device, reset=False):
    """Channels, config and notification rules for a (re)paired device."""
    n = devconfig.channel_count(device)
    existing = {c.idx for c in device.channels}
    for i in range(n):
        if i not in existing:
            device.channels.append(Channel(idx=i, label=f"CH{i + 1}"))
    if reset or not device.cfg or len(device.cfg.get("ch", [])) != n:
        device.cfg = devconfig.default_cfg(n)
    for ntype, _label in devconfig.NOTIFY_TYPES:
        rule = db.session.get(NotificationRule, (device.id, ntype))
        if rule is None:
            rule = NotificationRule(device_fk=device.id, type=ntype)
            db.session.add(rule)
        if reset or rule.enabled is None:
            rule.enabled = False
            rule.param = devconfig.DISCONNECTED_DEFAULT_MIN if ntype == "disconnected" else None


@bp.post("/api/v1/pair")
@limiter.limit("10 per minute")
def pair():
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return jsonify(err="bad_request"), 400
    code = normalize_code(body.get("code"))
    device_id = str(body.get("device_id", "")).strip().upper()
    if not DEVICE_ID_RE.match(device_id) or len(code) != 8:
        return jsonify(err="bad_request"), 400

    pc = db.session.get(PairingCode, code)
    if pc is None or pc.used_at is not None or pc.expires_at < now():
        return jsonify(err="invalid_code"), 404

    device = Device.query.filter_by(device_id=device_id).first()
    if device is not None and device.removed_at is None:
        return jsonify(err="already_paired"), 409

    caps = body.get("caps") if isinstance(body.get("caps"), dict) else {}
    if device is None:
        device = Device(device_id=device_id, name=device_id, caps=caps, cfg={}, pair_gen=1,
                        cfg_ver_wanted=1)
        db.session.add(device)
        db.session.flush()
    else:  # re-pairing hardware that was removed earlier
        device.removed_at = None
        device.pair_gen = (device.pair_gen or 1) + 1
        device.caps = caps
        device.cfg_ver_wanted = (device.cfg_ver_wanted or 0) + 1
        device.last_state = {}
        device.last_seq = 0

    secret = secrets.token_urlsafe(32)
    device.set_secret(secret)
    device.fw = str(body.get("fw", ""))[:32]
    device.hw = str(body.get("hw", ""))[:64]
    try:
        device.proto = int(body.get("proto", 1))
    except (TypeError, ValueError):
        device.proto = 1
    device.paired_at = now()
    device.cfg_ver_applied = 0
    device.cfg_error = None
    ensure_device_defaults(device, reset=True)
    pc.used_at = now()
    pc.device_fk = device.id
    ingest.server_event(device, "paired", {"fw": device.fw})
    db.session.commit()
    log.info("Device %s paired", device_id)
    return jsonify(secret=secret, ws=WS_PATH, time=now(),
                   config=devconfig.to_device(device.cfg, device.cfg_ver_wanted))


def _auth_device():
    """Return (device, close_code)."""
    header = request.headers.get("Authorization", "")
    if not header.startswith("Bearer ") or "." not in header:
        return None, 4401
    device_id, _, secret = header[7:].strip().partition(".")
    device = Device.query.filter_by(device_id=device_id.upper()).first()
    if device is None:
        return None, 4401
    if device.removed_at is not None:
        return device, 4403
    if not device.check_secret(secret):
        return None, 4401
    return device, None


@sock.route(WS_PATH, bp=bp)
def device_ws(ws):
    device, close_code = _auth_device()
    if close_code is not None:
        log.info("WS rejected (%s) for %s", close_code,
                 device.device_id if device else request.headers.get("Authorization", "")[:20])
        ws.close(reason=close_code, message="removed" if close_code == 4403 else "unauthorized")
        return
    conn = hub.Connection(device.id, ws)
    hub.register(conn)
    ingest.on_connect(device, conn)
    log.info("Device %s connected", device.device_id)
    last_rx = time.monotonic()
    reason = "connection_lost"
    try:
        while not conn.closed.is_set() and not hub.SHUTDOWN.is_set():
            while not conn.outbox.empty():
                ws.send(conn.outbox.get_nowait())
            data = ws.receive(timeout=0.5)
            if data is None:
                if time.monotonic() - last_rx > OFFLINE_AFTER_S:
                    reason = "ping_timeout"
                    break
                continue
            last_rx = time.monotonic()
            try:
                msg = json.loads(data)
            except (TypeError, ValueError):
                continue
            if not isinstance(msg, dict):
                continue
            try:
                ingest.handle(conn, device, msg)
            except Exception:
                log.exception("Error handling %s from %s", msg.get("t"), device.device_id)
                db.session.rollback()
            if device.removed_at is not None:
                conn.close(4403)
        while not conn.outbox.empty():  # flush (e.g. the `remove` command)
            ws.send(conn.outbox.get_nowait())
    except ConnectionClosed:
        pass
    finally:
        replaced = conn.close_code == 4000
        # On server shutdown the device stays marked online; the next start records an
        # "offline (server down)" incident from last_seen (ingest.mark_all_offline_on_startup).
        if hub.unregister(conn) and not replaced and not hub.SHUTDOWN.is_set():
            try:
                db.session.rollback()
                fresh = db.session.get(Device, device.id)
                if fresh is not None:
                    ingest.on_disconnect(fresh, reason)
            except Exception:
                log.exception("Error recording disconnect of %s", device.device_id)
        log.info("Device %s disconnected (%s)", device.device_id, reason)
        if conn.close_code and conn.close_code != 1000:
            try:
                ws.close(reason=conn.close_code)
            except Exception:
                pass
