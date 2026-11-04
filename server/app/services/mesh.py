"""Mesh (ESP-NOW): leaf meters without Wi-Fi reach the server through a gateway meter.

A gateway forwards each leaf message as
    {"t": "via", "d": <leaf device_id>, "n": <counter>, "h": <hmac>, "m": "<inner JSON text>"}
and the server answers the leaf the same way. The HMAC (key = the leaf's link_key, which the
gateway never has after pairing) covers device id, counter and message, so a gateway cannot
fake, change or replay a leaf's data, and cannot send it fake commands either.

Pairing a new leaf goes through a gateway too: {"t": "via_pair", "body": {...}} ->
{"t": "via_pair_res", "d": <leaf>, "code": <http status>, "resp": {...}}.
"""
import hashlib
import hmac
import itertools
import json
import logging
import threading
import time

from ..extensions import db
from ..models import Device
from . import hub, ingest

log = logging.getLogger(__name__)

LEAF_TIMEOUT_S = 75          # same as a direct connection (leaf pings every 30 s)
PAIR_RATE = (10, 60)         # max pairings per gateway per 60 s

_dn_lock = threading.Lock()
_dn_last = 0


def sign(key, device_id, n, m):
    msg = f"{device_id}|{n}|{m}".encode()
    return hmac.new(key.encode(), msg, hashlib.sha256).hexdigest()[:32]


def _next_down_nonce():
    """Downlink counter: milliseconds since epoch, strictly increasing."""
    global _dn_last
    with _dn_lock:
        _dn_last = max(_dn_last + 1, int(time.time() * 1000))
        return _dn_last


class LeafConnection:
    """Looks like hub.Connection to the rest of the server; sends through the gateway."""

    direct = False

    def __init__(self, device_pk, device_id, link_key, gateway):
        self.device_pk = device_pk
        self.device_id = device_id
        self.link_key = link_key
        self.gateway = gateway  # hub.Connection of the gateway meter
        self.closed = threading.Event()
        self.close_code = None
        self.boot = None
        self.boot_epoch = None
        self.last_rx = time.monotonic()
        self._ids = itertools.count(1)

    def send(self, msg):
        msg = dict(msg)
        msg.setdefault("id", next(self._ids))
        inner = json.dumps(msg, separators=(",", ":"))
        n = _next_down_nonce()
        self.gateway.send({"t": "via", "d": self.device_id, "n": n, "h": sign(self.link_key, self.device_id, n, inner),
                           "m": inner})
        return msg["id"]

    def close(self, code=1000):
        if code in (4401, 4403) and not self.closed.is_set():
            self.send({"t": "bye", "code": code})  # the leaf factory-resets / stops on 4403 / 4401
        self.close_code = code
        self.closed.set()


def _leaves(gw_conn):
    if not hasattr(gw_conn, "leaves"):
        gw_conn.leaves = {}
        gw_conn.pair_times = []
    return gw_conn.leaves


def handle(gw_conn, gw_device, msg):
    """Handle a gateway message. Returns True if it was a mesh message."""
    t = msg.get("t")
    if t == "via":
        _via(gw_conn, gw_device, msg)
        return True
    if t == "via_pair":
        _via_pair(gw_conn, gw_device, msg)
        return True
    return False


def _via(gw_conn, gw_device, msg):
    dev_id = str(msg.get("d", "")).upper()
    m, h = msg.get("m"), str(msg.get("h", ""))
    try:
        n = int(msg.get("n"))
    except (TypeError, ValueError):
        return
    if not isinstance(m, str) or dev_id == gw_device.device_id:
        return
    leaf = Device.query.filter_by(device_id=dev_id).first()
    if leaf is None or not leaf.link_key:
        log.info("Mesh: unknown leaf %s via %s", dev_id, gw_device.device_id)
        return
    if not hmac.compare_digest(h, sign(leaf.link_key, dev_id, n, m)):
        log.warning("Mesh: bad signature from leaf %s via %s (dropped)", dev_id, gw_device.device_id)
        return
    if n <= (leaf.link_up or 0):
        log.warning("Mesh: replayed/old message from leaf %s via %s (dropped)", dev_id, gw_device.device_id)
        return
    leaf.link_up = n
    leaves = _leaves(gw_conn)
    conn = leaves.get(leaf.id)
    if conn is None or conn.closed.is_set():
        conn = LeafConnection(leaf.id, dev_id, leaf.link_key, gw_conn)
        leaves[leaf.id] = conn
        if leaf.removed_at is not None:  # removed/unsynced while away: tell it to reset
            db.session.commit()
            conn.close(4403)
            return
        hub.register(conn)
        leaf.via_fk = gw_device.id
        ingest.on_connect(leaf, conn)
        log.info("Mesh: leaf %s connected via %s", dev_id, gw_device.device_id)
    conn.last_rx = time.monotonic()
    try:
        inner = json.loads(m)
    except ValueError:
        db.session.commit()
        return
    if isinstance(inner, dict):
        if leaf.removed_at is not None:
            conn.close(4403)
            db.session.commit()
            return
        ingest.handle(conn, leaf, inner)
    db.session.commit()


def _via_pair(gw_conn, gw_device, msg):
    from ..routes.device_api import pair_device  # avoid an import cycle
    _leaves(gw_conn)
    body = msg.get("body") if isinstance(msg.get("body"), dict) else None
    dev_id = str((body or {}).get("device_id", "")).upper()
    t = time.monotonic()
    gw_conn.pair_times = [x for x in gw_conn.pair_times if t - x < PAIR_RATE[1]]
    if len(gw_conn.pair_times) >= PAIR_RATE[0]:
        status, resp = 429, {"err": "rate_limited"}
    else:
        gw_conn.pair_times.append(t)
        body = dict(body or {})
        body["_via"] = gw_device.device_id
        status, resp = pair_device(body)
    gw_conn.send({"t": "via_pair_res", "d": dev_id, "code": status, "resp": resp})


def sweep(gw_conn):
    """Drop leaves that went silent (call regularly from the gateway's loop)."""
    leaves = _leaves(gw_conn)
    for pk, conn in list(leaves.items()):
        if conn.closed.is_set():  # replaced (e.g. the leaf is back on Wi-Fi) or told to reset
            if hub.unregister(conn):
                _offline(pk, "removed" if conn.close_code == 4403 else "connection_lost")
            leaves.pop(pk, None)
        elif time.monotonic() - conn.last_rx > LEAF_TIMEOUT_S:
            leaves.pop(pk, None)
            if hub.unregister(conn):
                _offline(pk, "ping_timeout")


def gateway_gone(gw_conn):
    """The gateway disconnected: its leaves are offline until they find another gateway."""
    for pk, conn in list(_leaves(gw_conn).items()):
        conn.closed.set()
        if hub.unregister(conn):
            _offline(pk, "gateway_lost")
    gw_conn.leaves = {}


def _offline(pk, reason):
    try:
        leaf = db.session.get(Device, pk)
        if leaf is not None:
            ingest.on_disconnect(leaf, reason)
    except Exception:
        log.exception("Mesh: error recording disconnect of leaf %s", pk)
        db.session.rollback()
    finally:
        db.session.remove()


def via_name(device):
    """Name of the gateway a meter is currently connected through, or None (direct / offline)."""
    conn = hub.get(device.id)
    if isinstance(conn, LeafConnection):
        gw = db.session.get(Device, conn.gateway.device_pk)
        return gw.name if gw else "?"
    return None
