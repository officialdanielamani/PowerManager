#!/usr/bin/env python3
"""Simulated PowerManager meter for testing the server without hardware.

Implements the device side of docs/protocol.md closely enough to exercise pairing, telemetry,
events, config and commands, including the offline buffer and the local limit rules.

    pip install simple-websocket
    python tools/sim_meter.py pair --server http://localhost:5000 --code K7P2-9QXM
    python tools/sim_meter.py run --load 900 --script "30:trip,60:tamper_open,90:tamper_close"

Interactive commands while running (type + Enter):
    load <W>   trip   tamper_open   tamper_close   ac_lost   ac_restored   send   quit
"""
import argparse
import json
import os
import random
import sys
import threading
import time
import urllib.error
import urllib.request

try:
    from simple_websocket import Client, ConnectionClosed
except ImportError:  # pragma: no cover
    sys.exit("pip install simple-websocket")

STATE_FILE = "sim_meter.json"


def load_state(path):
    if os.path.exists(path):
        with open(path) as f:
            return json.load(f)
    return {}


def save_state(path, st):
    with open(path, "w") as f:
        json.dump(st, f, indent=2)


def cmd_pair(args):
    st = load_state(args.state)
    st.setdefault("device_id", args.device_id or "SIM" + "".join(random.choice("ABCDEFGHJKMNPQRSTUVWXYZ23456789") for _ in range(5)))
    body = {"code": args.code, "device_id": st["device_id"], "fw": "sim-0.1", "hw": "simulator", "proto": 1,
            "caps": {"sensor": "pzem004t", "channels": 1, "relays": 1, "inputs": ["tamper"], "rtc": False,
                     "amp_rating": 100}}
    req = urllib.request.Request(args.server.rstrip("/") + "/api/v1/pair", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            resp = json.loads(r.read())
    except urllib.error.HTTPError as e:
        sys.exit(f"Pair failed: HTTP {e.code} {e.read().decode()}")
    st.update(server=args.server.rstrip("/"), secret=resp["secret"], ws=resp["ws"],
              cfg=resp["config"]["cfg"], cfg_ver=resp["config"]["ver"], seq=st.get("seq", 0), boot=0,
              sid=0, e_total=st.get("e_total", 12.0))
    save_state(args.state, st)
    print(f"Paired as {st['device_id']}. Config v{st['cfg_ver']}: {json.dumps(st['cfg'])}")


class Meter:
    def __init__(self, args):
        self.args = args
        self.st = load_state(args.state)
        if "secret" not in self.st:
            sys.exit("Not paired. Run: sim_meter.py pair --server URL --code CODE")
        self.st["boot"] = self.st.get("boot", 0) + 1
        self.boot_t = time.time()
        self.load_w = args.load
        self.lock = threading.Lock()
        self.buffer = []  # ordered queue of items/events waiting for ack
        self.ws = None
        self.want_on = True
        self.tripped = self.st.get("tripped", False)
        self.tamper = False
        self.ac = True
        self.offline_since = None
        self.sess = self.st.get("sess") or {}
        self.last_report = 0
        self.running = True
        self.reading = None
        if not self.sess:
            self.new_session(auto=False, reason=None)
        save_state(args.state, self.st)

    # ------------------------------------------------------------ helpers
    def next_seq(self):
        self.st["seq"] = self.st.get("seq", 0) + 1
        return self.st["seq"]

    def cfg(self):
        return self.st.get("cfg") or {}

    def ch_cfg(self):
        chs = self.cfg().get("ch") or [{}]
        return chs[0]

    def log(self, *a):
        print(time.strftime("%H:%M:%S"), *a, flush=True)

    def event(self, e, **data):
        item = {"t": "evt", "seq": self.next_seq(), "ts": int(time.time()), "tv": 1, "e": e, **data}
        with self.lock:
            self.buffer.append(item)
        self.log("EVENT", e, data)

    def session_status(self):
        return self.sess.get("status", "active")

    def next_reset(self):
        s = self.cfg().get("sess") or {}
        mode = s.get("mode", "none")
        if mode == "duration":
            return self.sess["start"] + int(s.get("dur_min", 1440)) * 60
        if mode == "daily":
            m = int(s.get("daily_utc_min", 0))
            now = int(time.time())
            day = now - now % 86400
            t = day + m * 60
            if t <= self.sess["start"]:
                t += 86400
            return t
        return None

    def new_session(self, auto, reason):
        if self.sess and reason:
            self.event("session_end", sid=self.sess["sid"], reason=reason, kwh=round(self.session_kwh(), 4))
        self.st["sid"] = self.st.get("sid", 0) + 1
        self.sess = {"sid": self.st["sid"], "start": int(time.time()), "e0": self.st.get("e_total", 0.0),
                     "status": "active"}
        self.st["sess"] = self.sess
        self.event("session_start", sid=self.sess["sid"], auto=auto, max=self.ch_cfg().get("kwh_max", 0))

    def session_kwh(self):
        return self.st.get("e_total", 0.0) - self.sess.get("e0", 0.0)

    def blocked(self):
        if self.tripped:
            return "tripped"
        if self.session_status() == "quota":
            return "quota"
        if self.session_status() == "expired":
            return "expired"
        off_min = self.cfg().get("offline_off_min", 0)
        if off_min and self.offline_since and time.time() - self.offline_since >= off_min * 60:
            return "offline"
        if not self.ac:
            return "ac_lost"
        return None

    def relay_on(self):
        return self.want_on and self.blocked() in (None, "ac_lost")

    def state_msg(self):
        nr = self.next_reset()
        return {"relay": [{"i": 0, "on": self.relay_on(), "want": self.want_on, "block": self.blocked()}],
                "tripped": self.tripped, "tamper": self.tamper, "ac": self.ac,
                "sess": {"sid": self.sess["sid"], "start": self.sess["start"], "kwh": round(self.session_kwh(), 4),
                         "max": self.ch_cfg().get("kwh_max", 0), "status": self.session_status(),
                         **({"next_reset": nr} if nr else {})}}

    # ------------------------------------------------------------ control loop (1 s)
    def control(self):
        prev_state = None
        while self.running:
            time.sleep(1 * self.args.speed)
            if self.ac:
                w = self.load_w * random.uniform(0.95, 1.05) if self.relay_on() else 0.0
                v = random.uniform(228, 234)
                a = w / v if v else 0
                # energy: speed factor makes simulated time pass faster for testing quotas
                self.st["e_total"] = self.st.get("e_total", 0.0) + w / 1000 / 3600 * self.args.energy_x
                self.reading = {"i": 0, "v": round(v, 1), "a": round(a, 3), "w": round(w, 1), "pf": 0.97,
                                "hz": 50.0, "e": round(self.st["e_total"], 4)}
                amp_max = self.ch_cfg().get("amp_max", 0)
                if amp_max and a > amp_max and not self.tripped:
                    self.tripped = True
                    self.st["tripped"] = True
                    self.event("overcurrent", ch=0, val=round(a, 2), limit=amp_max)
            else:
                self.reading = None
            kmax = self.ch_cfg().get("kwh_max", 0)
            if kmax and self.session_status() == "active" and self.session_kwh() >= kmax:
                self.sess["status"] = "quota"
                self.event("quota", ch=0, kwh=round(self.session_kwh(), 4))
            nr = self.next_reset()
            if nr and time.time() >= nr and self.session_status() != "expired":
                if (self.cfg().get("sess") or {}).get("auto"):
                    self.new_session(auto=True, reason="expired")
                else:
                    if self.session_status() == "active" or self.session_status() == "quota":
                        self.sess["status"] = "expired"
                        self.event("session_end", sid=self.sess["sid"], reason="expired",
                                   kwh=round(self.session_kwh(), 4))
            if time.time() - self.last_report >= self.cfg().get("report_s", 60) * self.args.speed:
                self.report()
            # like the firmware: send "state" only on real changes, not on every kWh tick
            st = self.state_msg()
            st["sess"] = {k: v for k, v in st["sess"].items() if k != "kwh"}
            cur = json.dumps(st, sort_keys=True)
            if cur != prev_state:
                prev_state = cur
                self.send({"t": "state", **self.state_msg()})
            save_state(self.args.state, self.st)

    def report(self):
        self.last_report = time.time()
        item = {"seq": self.next_seq(), "ts": int(time.time()), "tv": 1,
                "ch": [self.reading] if self.reading else []}
        with self.lock:
            self.buffer.append({"t": "tel", "items": [item]})
            # keep at most N buffered entries (oldest dropped), like the device ring buffer
            if len(self.buffer) > self.args.buffer:
                self.buffer = self.buffer[-self.args.buffer:]

    # ------------------------------------------------------------ network
    def send(self, msg):
        ws = self.ws
        if ws is None:
            return False
        try:
            ws.send(json.dumps(msg))
            return True
        except Exception:
            return False

    def flush(self):
        # Send the buffer, then wait for an ack (resend after 5 s if none arrives).
        if time.time() - getattr(self, "_last_flush", 0) < 5 and getattr(self, "_flushed_n", 0) >= len(self.buffer):
            return
        with self.lock:
            pending = list(self.buffer)
        self._last_flush, self._flushed_n = time.time(), len(pending)
        if not pending:
            return
        # merge consecutive telemetry into batches of up to 50 items
        batch = []
        for m in pending:
            if m["t"] == "tel":
                batch.extend(m["items"])
                continue
            if batch:
                self.send({"t": "tel", "items": batch[:50]})
                batch = []
            self.send(m)
        if batch:
            self.send({"t": "tel", "items": batch[:50]})

    def on_ack(self, last_seq):
        with self.lock:
            keep = []
            for m in self.buffer:
                top = max(i["seq"] for i in m["items"]) if m["t"] == "tel" else m["seq"]
                if top > last_seq:
                    keep.append(m)
            self.buffer = keep
        self._flushed_n = 0 if self.buffer else self._flushed_n
        self._last_flush = 0 if self.buffer else self._last_flush

    def handle(self, msg):
        t = msg.get("t")
        if t == "welcome":
            self.log("WELCOME", msg)
        elif t == "ack":
            self.on_ack(msg.get("last_seq", 0))
            self.log(f"ACK last_seq={msg.get('last_seq')} (green x2), buffer={len(self.buffer)}")
        elif t == "cfg":
            self.st["cfg"], self.st["cfg_ver"] = msg["cfg"], msg["ver"]
            self.send({"t": "cfg_ack", "ver": msg["ver"], "ok": True})
            self.log("CONFIG applied v", msg["ver"], json.dumps(msg["cfg"]))
        elif t == "cmd":
            self.command(msg)
        elif t == "pong":
            pass
        elif t == "bye":
            self.log("BYE", msg.get("code"))
            if msg.get("code") == 4403:
                self.factory_reset()

    def command(self, msg):
        c, ref = msg.get("c"), msg.get("ref")
        ok, err = True, None
        if c == "relay":
            if msg.get("on") and self.blocked() in ("tripped", "quota", "expired"):
                ok, err = False, {"tripped": "tripped", "quota": "quota_exceeded", "expired": "session_expired"}[self.blocked()]
            else:
                self.want_on = bool(msg.get("on"))
                self.event("relay", on=self.want_on, src="server")
        elif c == "renew":
            self.new_session(auto=False, reason="manual")
        elif c == "reset_trip":
            if self.tripped:
                self.tripped = False
                self.st["tripped"] = False
                self.event("trip_reset", src="server", by=msg.get("by"))
        elif c == "send_now":
            self.report()
        elif c == "get_state":
            self.send({"t": "state", **self.state_msg()})
        elif c == "remove":
            self.send({"t": "cmd_ack", "ref": ref, "ok": True})
            self.factory_reset()
            return
        else:
            ok, err = False, "unknown_cmd"
        self.send({"t": "cmd_ack", "ref": ref, "ok": ok, **({"err": err} if err else {})})
        self.log("CMD", c, "ok" if ok else err)

    def factory_reset(self):
        self.log("FACTORY RESET (removed by server)")
        if os.path.exists(self.args.state):
            os.remove(self.args.state)
        self.running = False

    def ws_url(self):
        base = self.st["server"].replace("https://", "wss://").replace("http://", "ws://")
        return base + self.st.get("ws", "/ws/device")

    def net(self):
        backoff = 2
        while self.running:
            try:
                headers = {"Authorization": f"Bearer {self.st['device_id']}.{self.st['secret']}", "X-PM-Proto": "1"}
                self.ws = Client.connect(self.ws_url(), headers=headers)
                if self.offline_since:
                    self.event("server_back", since=int(self.offline_since))
                self.offline_since = None
                backoff = 2
                self.log("CONNECTED")
                self.send({"t": "hello", "fw": "sim-0.1", "proto": 1, "boot": self.st["boot"],
                           "up": int(time.time() - self.boot_t), "tv": 1, "ts": int(time.time()),
                           "cfg_ver": self.st.get("cfg_ver", 0), "caps": {"channels": 1}, "state": self.state_msg()})
                last_ping = time.time()
                while self.running:
                    self.flush()
                    data = self.ws.receive(timeout=1)
                    if data:
                        self.handle(json.loads(data))
                    if time.time() - last_ping > 30:
                        self.send({"t": "ping"})
                        last_ping = time.time()
            except ConnectionClosed as e:
                code = getattr(e, "reason", None)
                self.log("DISCONNECTED", code)
                if code == 4403:
                    self.factory_reset()
                    return
                if code == 4401:
                    self.log("Not paired (4401) - stopping")
                    self.running = False
                    return
            except Exception as e:
                self.log("NET ERROR", type(e).__name__, e)
            self.ws = None
            if self.offline_since is None:
                self.offline_since = time.time()
                self.event("server_lost")
            self.log(f"OFFLINE (red blink) - retry in {backoff}s, buffer={len(self.buffer)}")
            time.sleep(backoff)
            backoff = min(60, backoff * 2)

    def scripted(self, script):
        steps = []
        for part in filter(None, (script or "").split(",")):
            t, action = part.split(":", 1)
            steps.append((float(t), action))
        start = time.time()
        for t, action in sorted(steps):
            time.sleep(max(0, start + t - time.time()))
            self.do(action)

    def do(self, line):
        parts = line.strip().split()
        if not parts:
            return
        c = parts[0]
        if c == "load" and len(parts) > 1:
            self.load_w = float(parts[1])
        elif c == "trip":
            self.load_w = (self.ch_cfg().get("amp_max", 10) + 2) * 230
            self.log("Load spike ->", self.load_w, "W")
        elif c == "tamper_open" and not self.tamper:
            self.tamper = True
            self.event("tamper_open")
        elif c == "tamper_close" and self.tamper:
            self.tamper = False
            self.event("tamper_close")
        elif c == "ac_lost" and self.ac:
            self.ac = False
            self.event("ac_lost")
        elif c == "ac_restored" and not self.ac:
            self.ac = True
            self.event("ac_restored")
        elif c == "send":
            self.report()
        elif c == "quit":
            self.running = False

    def run(self):
        threading.Thread(target=self.control, daemon=True).start()
        threading.Thread(target=self.net, daemon=True).start()
        if self.args.script:
            threading.Thread(target=self.scripted, args=(self.args.script,), daemon=True).start()
        if sys.stdin.isatty():
            threading.Thread(target=lambda: [self.do(l) for l in sys.stdin], daemon=True).start()
        end = time.time() + self.args.duration if self.args.duration else None
        while self.running and (end is None or time.time() < end):
            time.sleep(0.5)
        self.running = False
        if self.ws:
            try:
                self.ws.close()
            except Exception:
                pass


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--state", default=STATE_FILE, help="state file (one per simulated meter)")
    sub = p.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("pair")
    a.add_argument("--server", required=True, help="e.g. http://localhost:5000")
    a.add_argument("--code", required=True)
    a.add_argument("--device-id")
    b = sub.add_parser("run")
    b.add_argument("--load", type=float, default=800, help="load in W while relay is ON")
    b.add_argument("--script", help='timed actions, e.g. "20:trip,40:tamper_open"')
    b.add_argument("--duration", type=float, default=0, help="stop after N seconds (0 = forever)")
    b.add_argument("--speed", type=float, default=1.0, help="<1 makes loop/report faster")
    b.add_argument("--energy-x", type=float, default=1.0, help="multiply energy accumulation (test quotas)")
    b.add_argument("--buffer", type=int, default=500, help="offline buffer size")
    args = p.parse_args()
    if args.cmd == "pair":
        cmd_pair(args)
    else:
        Meter(args).run()


if __name__ == "__main__":
    main()
