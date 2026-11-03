"""Registry of live meter WebSocket connections.

Each connection has an outbound queue. Only the connection's own thread writes to the socket
(simple-websocket is not safe for concurrent sends), so other threads call `send()`.
"""
import itertools
import json
import queue
import threading

_lock = threading.Lock()
_conns = {}  # device pk -> Connection
_ids = itertools.count(1)
SHUTDOWN = threading.Event()  # set when the server is stopping


class Connection:
    def __init__(self, device_pk, ws):
        self.device_pk = device_pk
        self.ws = ws
        self.outbox = queue.Queue()
        self.closed = threading.Event()
        self.boot_epoch = None  # server-estimated epoch of the device's current boot
        self.boot = None
        self.close_code = None

    def send(self, msg):
        msg = dict(msg)
        msg.setdefault("id", next(_ids))
        self.outbox.put(json.dumps(msg, separators=(",", ":")))
        return msg["id"]

    def close(self, code=1000):
        self.close_code = code
        self.closed.set()


def register(conn):
    with _lock:
        old = _conns.get(conn.device_pk)
        _conns[conn.device_pk] = conn
    if old is not None:
        old.close(4000)  # replaced by a newer connection


def unregister(conn):
    with _lock:
        if _conns.get(conn.device_pk) is conn:
            del _conns[conn.device_pk]
            return True
    return False


def get(device_pk):
    with _lock:
        return _conns.get(device_pk)


def is_online(device_pk):
    return get(device_pk) is not None


def send(device_pk, msg):
    conn = get(device_pk)
    if conn is None:
        return None
    return conn.send(msg)


def disconnect(device_pk, code):
    conn = get(device_pk)
    if conn is not None:
        conn.close(code)


def shutdown():
    """Close every meter connection (server is stopping). Devices reconnect later."""
    SHUTDOWN.set()
    with _lock:
        conns = list(_conns.values())
    for conn in conns:
        conn.close(1001)


def install_signal_handler():
    """Chain onto gunicorn's SIGTERM/SIGINT handlers so WS threads stop before the
    graceful timeout (otherwise the worker is SIGKILLed while threads are still looping)."""
    import signal

    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGQUIT):
        try:
            previous = signal.getsignal(sig)
        except ValueError:
            continue

        def handler(signum, frame, _prev=previous):
            shutdown()
            if callable(_prev):
                _prev(signum, frame)

        try:
            signal.signal(sig, handler)
        except ValueError:  # not in main thread (e.g. some test runners)
            return
