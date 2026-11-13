"""MQTT link to the meters: the same JSON protocol as the meters' WebSocket, carried through an MQTT broker.

    <prefix>/<DEVICE_ID>/up      meter -> server       <prefix>/<DEVICE_ID>/down    server -> meter
    <prefix>/<DEVICE_ID>/status  "online" / "offline" (retained; "offline" is the meter's last will)

Every message is wrapped as {"n": counter, "h": hmac, "m": "<protocol JSON text>"} and signed with the meter's
link key (given at pairing, the same key and envelope as the mesh). So somebody who only has the broker login
cannot fake a meter, replay its data or send it commands. The broker (Settings -> MQTT, admin) may sit behind
Cloudflare: the server connects over TCP or WebSocket, the meters always over WebSocket.
"""
import hmac
import itertools
import json
import logging
import ssl
import threading
import time

from ..extensions import db
from ..models import Device
from . import hub, ingest, mesh, settings

log = logging.getLogger(__name__)

OFFLINE_AFTER_S = 75   # same as a WebSocket connection (meters ping every 30 s)
_app = None
_client = None
_lock = threading.Lock()
_msg_lock = threading.RLock()  # messages (broker thread) and the sweep (bridge thread) take turns
_conns = {}            # device pk -> MqttConnection
status = {"state": "off", "error": "", "since": 0}


def _set_status(state, error=""):
    status.update(state=state, error=error, since=int(time.time()))


def default_port(transport, tls):
    if transport == "websockets":
        return 443 if tls else 80
    return 8883 if tls else 1883


def server_config():
    """How the server itself connects to the broker."""
    try:
        port = int(settings.get("mqtt_port") or 0)
    except ValueError:
        port = 0
    tls = settings.get("mqtt_tls") == "1"
    transport = "websockets" if settings.get("mqtt_transport") != "tcp" else "tcp"
    return {"enabled": settings.get("mqtt_enabled") == "1", "host": settings.get("mqtt_host").strip(),
            "port": port or default_port(transport, tls), "tls": tls,
            "transport": transport, "path": settings.get("mqtt_path") or "/",
            "user": settings.get("mqtt_user"), "pass": settings.get("mqtt_pass"),
            "prefix": (settings.get("mqtt_prefix") or "powermanager").strip("/")}


def meter_config():
    """What a meter gets at pairing ("auto link") / with set_mqtt: the broker as seen from the meters (always
    MQTT over WebSocket). None when MQTT is off or the server has no WebSocket address for the meters."""
    c = server_config()
    if not c["enabled"] or not c["host"]:
        return None
    host = settings.get("mqtt_pub_host").strip() or (c["host"] if c["transport"] == "websockets" else "")
    if not host:
        return None  # server on plain TCP and no public WebSocket address given: meters keep the WebSocket
    own = bool(settings.get("mqtt_pub_host").strip())
    tls = (settings.get("mqtt_pub_tls") == "1") if own else c["tls"]
    try:
        port = int(settings.get("mqtt_pub_port") or 0) if own else c["port"]
    except ValueError:
        port = 0
    return {"enabled": True, "host": host, "port": port or (443 if tls else 80), "tls": tls,
            "path": (settings.get("mqtt_pub_path") or "/") if own else c["path"],
            "user": c["user"], "pass": c["pass"], "prefix": c["prefix"]}


def device_mqtt_doc(device):
    """The set_mqtt parameters for one meter from its Connection choice (server broker / off / custom)."""
    net = device.net or {}
    mode = net.get("mqtt", "server")
    if mode == "custom" and net.get("host"):
        return {"enabled": True, "host": net["host"], "port": int(net.get("port") or 443), "tls": bool(net.get("tls", True)),
                "path": net.get("path") or "/", "user": net.get("user", ""), "pass": net.get("pass", ""),
                "prefix": net.get("prefix") or server_config()["prefix"]}
    if mode == "off":
        return {"enabled": False}
    return meter_config() or {"enabled": False}


# ---------------------------------------------------------------- connection objects

class MqttConnection:
    """Looks like hub.Connection to the rest of the server; sends through the broker."""

    direct = True

    def __init__(self, device_pk, device_id, link_key, prefix):
        self.device_pk = device_pk
        self.device_id = device_id
        self.link_key = link_key
        self.topic = f"{prefix}/{device_id}/down"
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
        n = mesh._next_down_nonce()
        payload = json.dumps({"n": n, "h": mesh.sign(self.link_key, self.device_id, n, inner), "m": inner},
                             separators=(",", ":"))
        client = _client
        if client is None:
            return None
        client.publish(self.topic, payload, qos=0)
        return msg["id"]

    def close(self, code=1000):
        if code in (4401, 4403) and not self.closed.is_set():
            self.send({"t": "bye", "code": code})  # the meter factory-resets / stops on 4403 / 4401
        self.close_code = code
        self.closed.set()


def _drop(conn, reason):
    """The meter is gone from the broker (last will, silence, replaced)."""
    _conns.pop(conn.device_pk, None)
    if getattr(conn, "leaves", None):
        mesh.gateway_gone(conn)
    replaced = conn.close_code == 4000
    if hub.unregister(conn) and not replaced:
        device = db.session.get(Device, conn.device_pk)
        if device is not None:
            ingest.on_disconnect(device, "removed" if conn.close_code == 4403 else reason)


# ---------------------------------------------------------------- incoming messages

def handle_message(topic, payload, prefix=None):
    """One MQTT message (called by the client thread inside an app context; also used by the tests)."""
    with _msg_lock:
        _handle_message(topic, payload, prefix or server_config()["prefix"])


def _handle_message(topic, payload, prefix):
    parts = topic[len(prefix) + 1:].split("/") if topic.startswith(prefix + "/") else []
    if len(parts) != 2:
        return
    dev_id, leaf = parts[0].upper(), parts[1]
    device = Device.query.filter_by(device_id=dev_id).first()
    if device is None or not device.link_key:
        return
    if leaf == "status":
        conn = _conns.get(device.id)
        if conn is not None and payload.strip() == b"offline":
            _drop(conn, "connection_lost")
            db.session.commit()
        return
    if leaf != "up":
        return
    try:
        env = json.loads(payload)
        n, h, m = int(env["n"]), str(env["h"]), env["m"]
    except (ValueError, KeyError, TypeError):
        return
    if not isinstance(m, str) or not hmac.compare_digest(h, mesh.sign(device.link_key, dev_id, n, m)):
        log.warning("MQTT: bad signature for %s (dropped)", dev_id)
        return
    if n <= (device.link_up or 0):
        log.warning("MQTT: replayed/old message for %s (dropped)", dev_id)
        return
    device.link_up = n
    try:
        msg = json.loads(m)
    except ValueError:
        db.session.commit()
        return
    if not isinstance(msg, dict):
        db.session.commit()
        return
    conn = _conns.get(device.id)
    hello = msg.get("t") == "hello"
    if conn is None or conn.closed.is_set() or hello:  # hello = a new connection of the meter
        if conn is not None:
            conn.close_code = 4000
            conn.closed.set()
        conn = MqttConnection(device.id, dev_id, device.link_key, prefix)
        _conns[device.id] = conn
        if device.removed_at is not None:  # removed / unsynced while away: tell it to reset
            db.session.commit()
            conn.close(4403)
            _conns.pop(device.id, None)
            return
        hub.register(conn)
        device.via_fk = None
        ingest.on_connect(device, conn)
        log.info("Device %s connected (MQTT)", dev_id)
        if not hello:  # the server restarted while the meter stayed on the broker: ask for its hello
            conn.send({"t": "rehello"})
    conn.last_rx = time.monotonic()
    if device.removed_at is not None:
        conn.close(4403)
        _drop(conn, "removed")
        db.session.commit()
        return
    if not mesh.handle(conn, device, msg):  # messages relayed for mesh leaves of this meter
        ingest.handle(conn, device, msg)
    db.session.commit()


def sweep():
    """Meters that went silent without a last will, and mesh leaves behind MQTT gateways."""
    with _msg_lock:
        for conn in list(_conns.values()):
            if getattr(conn, "leaves", None):
                mesh.sweep(conn)
            if conn.closed.is_set() or time.monotonic() - conn.last_rx > OFFLINE_AFTER_S:
                _drop(conn, "connection_lost" if conn.closed.is_set() else "ping_timeout")
        db.session.commit()


# ---------------------------------------------------------------- broker client

def _make_client(cfg, client_id):
    import paho.mqtt.client as mqtt
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=client_id, transport=cfg["transport"])
    if cfg["transport"] == "websockets":
        client.ws_set_options(path=cfg["path"] or "/")
    if cfg["tls"]:
        client.tls_set(cert_reqs=ssl.CERT_REQUIRED)
    if cfg["user"]:
        client.username_pw_set(cfg["user"], cfg["pass"] or None)
    return client


def test_connection(cfg, timeout=8):
    """Try the broker settings (Settings -> MQTT -> Test connection). Returns (ok, message)."""
    if not cfg.get("host"):
        return False, "Enter the broker address first"
    done = threading.Event()
    result = {"ok": False, "msg": "No answer from the broker (address, port or TLS wrong?)"}

    def on_connect(client, userdata, flags, reason_code, properties=None):
        ok = not getattr(reason_code, "is_failure", reason_code != 0)
        result.update(ok=ok, msg="Connected to the broker" if ok else f"The broker refused the login: {reason_code}")
        done.set()

    def on_fail(client, userdata):
        done.set()

    try:
        client = _make_client(cfg, f"pm-test-{int(time.time())}")
        client.on_connect = on_connect
        client.on_connect_fail = on_fail
        client.connect_async(cfg["host"], cfg["port"], keepalive=20)
        client.loop_start()
        done.wait(timeout)
        client.disconnect()
        client.loop_stop()
    except Exception as e:  # bad host name, TLS error ...
        return False, f"Cannot connect: {e}"
    return result["ok"], result["msg"]


def _run(app):
    """Client thread: (re)connect whenever the settings change; sweep silent meters."""
    global _client
    current, last_sweep = None, 0.0
    while not hub.SHUTDOWN.is_set():
        with app.app_context():
            try:
                cfg = server_config()
                want = cfg if cfg["enabled"] and cfg["host"] else None
                if want != current:
                    with _lock:
                        if _client is not None:
                            _client.loop_stop()
                            _client.disconnect()
                            _client = None
                    current = want
                    if want is None:
                        _set_status("off")
                    else:
                        _set_status("connecting")
                        _client = _connect(app, want)
                if time.monotonic() - last_sweep > 5:
                    last_sweep = time.monotonic()
                    sweep()
            except Exception:
                log.exception("MQTT bridge error")
                db.session.rollback()
            finally:
                db.session.remove()
        hub.SHUTDOWN.wait(2)
    if _client is not None:
        _client.loop_stop()


def _connect(app, cfg):
    client = _make_client(cfg, f"pm-server-{int(time.time()) % 100000}")
    prefix = cfg["prefix"]

    def on_connect(c, userdata, flags, reason_code, properties=None):
        if getattr(reason_code, "is_failure", reason_code != 0):
            _set_status("error", f"The broker refused the login: {reason_code}")
            return
        c.subscribe([(f"{prefix}/+/up", 0), (f"{prefix}/+/status", 0)])
        _set_status("connected")
        log.info("MQTT: connected to %s:%s (%s), topics %s/+/...", cfg["host"], cfg["port"], cfg["transport"], prefix)

    def on_disconnect(c, userdata, flags, reason_code, properties=None):
        if status["state"] == "connected":
            _set_status("error", f"Connection to the broker lost: {reason_code}")

    def on_message(c, userdata, m):
        with app.app_context():
            try:
                handle_message(m.topic, m.payload, prefix)
            except Exception:
                log.exception("MQTT: error handling a message on %s", m.topic)
                db.session.rollback()
            finally:
                db.session.remove()

    client.on_connect, client.on_disconnect, client.on_message = on_connect, on_disconnect, on_message
    client.reconnect_delay_set(2, 60)
    try:
        client.connect_async(cfg["host"], cfg["port"], keepalive=45)
        client.loop_start()
    except Exception as e:
        _set_status("error", f"Cannot connect: {e}")
    return client


def start(app):
    global _app
    _app = app
    threading.Thread(target=_run, args=(app,), name="mqtt-bridge", daemon=True).start()


def push_settings_to_meters():
    """The broker settings changed: meters that follow the server's broker get the new ones (queued while
    a meter is offline; a meter whose broker stopped working falls back to the WebSocket and gets them there)."""
    from . import commands
    n = 0
    for d in Device.query.filter(Device.removed_at.is_(None)).all():
        if (d.caps or {}).get("mqtt") and (d.net or {}).get("mqtt", "server") == "server":
            commands.issue(d, "set_mqtt", device_mqtt_doc(d))
            n += 1
    return n
