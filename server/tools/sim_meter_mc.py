#!/usr/bin/env python3
"""Simulated multi-channel AC meter (rbAmp firmware, docs/multichannel.md) for testing the server.

Follows the firmware's rules: shared / independent sessions, per-channel relays, trips and kWh
limits, End / Renew per channel. No mesh, offline buffer only in memory.

    python tools/sim_meter_mc.py --state mc1.json pair --server http://localhost:5000 --code K7P29QXM --channels 2
    python tools/sim_meter_mc.py --state mc1.json run --load 600,1200 --energy-x 200

Hardware profile (docs/protocol.md §12): the simulator answers i2c_scan, rbamp_addr, hw_profile and hw_cancel,
restarts after the delay and rolls back when a listed address does not answer. `--bus 0x50,0x20` sets which
I2C addresses "answer" (default: the profile's rbAmp and relay boards + 0x68).

Interactive commands while running (type + Enter):
    load <ch> <W>   trip <ch>   tamper_open   tamper_close   send   bus add|del <addr>   quit
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
    st.setdefault("device_id", args.device_id or "MC" + "".join(random.choice("ABCDEFGHJKMNPQRSTUVWXYZ23456789") for _ in range(6)))
    st["nch"] = args.channels
    body = {"code": args.code, "device_id": st["device_id"], "fw": "sim-mc-0.7", "hw": "simulator", "proto": 1,
            "caps": caps(args.channels)}
    req = urllib.request.Request(args.server.rstrip("/") + "/api/v1/pair", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            resp = json.loads(r.read())
    except urllib.error.HTTPError as e:
        sys.exit(f"Pair failed: HTTP {e.code} {e.read().decode()}")
    st.update(server=args.server.rstrip("/"), secret=resp["secret"], ws=resp["ws"], cfg=resp["config"]["cfg"],
              cfg_ver=resp["config"]["ver"], seq=0, boot=0, sid_counter=0, started=False,
              e=[0.0] * args.channels, sess=[{} for _ in range(args.channels)],
              want=[True] * args.channels, tripped=[False] * args.channels)
    save_state(args.state, st)
    print(f"Paired as {st['device_id']} ({args.channels} channels). Config v{st['cfg_ver']}")


def default_profile(n):
    """What config.h compiled in: one rbAmp at 0x50, one XL9535 relay board at 0x20, channel k on input k, relay k."""
    return {"boards": [0x50], "relays": [{"drv": "xl9535", "addr": 0x20, "n": n, "hi": 1}],
            "ch": [{"b": 1, "i": k + 1, "a": 30, "rb": 1, "ro": k + 1} for k in range(n)]}


def validate_profile(p):
    """The server's rules when its code is importable (run from the server folder), else no check."""
    try:
        sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
        from app.services import hwprofile
    except Exception:  # pragma: no cover - flask etc. not installed
        return []
    return hwprofile.validate(p)


def caps(n, profile=None):
    c = {"kind": "ac", "mc": True, "sensor": "rbamp", "variant": f"UI{n}", "channels": n, "relays": n,
         "voltage": True, "drdy": True, "rtc": True, "amp_rating": 34.8, "ratings": [30] * n, "inputs": ["tamper"],
         "hw_cfg": 1}
    if profile:
        chans = profile["ch"]
        total = sum(r["n"] for r in profile["relays"])
        used = {(c_["rb"], c_["ro"]) for c_ in chans if c_["rb"]}
        c.update(channels=len(chans), relays=len(used), aux=total - len(used),
                 ratings=[c_["a"] or 30 for c_ in chans], variant=f"UI{len(chans)}")
    return c


class MeterMC:
    def __init__(self, args):
        self.args = args
        self.st = load_state(args.state)
        if "secret" not in self.st:
            sys.exit("Not paired: run 'pair' first")
        self.n = self.st["nch"]
        self.st["boot"] = self.st.get("boot", 0) + 1
        self.boot_t = time.time()
        self.lock = threading.RLock()
        self.buffer, self.ws, self.running = [], None, True
        loads = [float(x) for x in (args.load or "800").split(",")]
        self.load = (loads * self.n)[:self.n]
        self.relay_on = [False] * self.n
        self.tamper, self.disabled = False, self.st.get("disabled", False)
        self.relay_on = [False] * self.n
        self.block = [None] * self.n
        self.hwp = self.st.setdefault("hwp", {"ver": 0, "state": "default", "profile": default_profile(self.n),
                                              "prev": None})
        if self.hwp["state"] in ("trial", "pending"):  # the simulator was stopped in the middle of a change
            self.hwp.update(state="active" if self.hwp["ver"] else "default")
        self.resize(len(self.hwp["profile"]["ch"]))
        if args.bus:
            self.bus = {int(x, 0) for x in args.bus.split(",")}
        else:
            pr = self.hwp["profile"]
            self.bus = {0x68, *pr["boards"], *[r["addr"] for r in pr["relays"] if r["drv"] != "gpio"]}
        self.countdown = None   # (due epoch, wanted profile message) while a reboot is pending

    # ------------------------------------------------------------ helpers
    def log(self, *a):
        print(time.strftime("%H:%M:%S"), *a, flush=True)

    def resize(self, n):
        """The channel count changed (new hardware profile): keep what exists, add defaults."""
        old = self.n
        self.n = n
        st = self.st
        st["nch"] = n
        for key, fill in (("e", 0.0), ("want", True), ("tripped", False)):
            st[key] = (list(st[key]) + [fill] * n)[:n]
        st["sess"] = (list(st["sess"]) + [{} for _ in range(n)])[:n]
        self.load = (list(getattr(self, "load", [800.0])) + [self.load[0] if getattr(self, "load", None) else 800.0] * n)[:n]
        self.relay_on = (list(getattr(self, "relay_on", [])) + [False] * n)[:n]
        self.block = (list(getattr(self, "block", [])) + [None] * n)[:n]
        return old

    def cfg(self):
        return self.st.get("cfg") or {}

    def indep(self):
        return self.cfg().get("mode") == "independent"

    def slots(self):
        return self.n if self.indep() else 1

    def slot_of(self, ch):
        return ch if self.indep() else 0

    def slot_cfg(self, s):
        c = self.cfg()
        if self.indep():
            cc = (c.get("ch") or [{}] * self.n)[s] if s < len(c.get("ch") or []) else {}
            return cc.get("sess") or {}, float(cc.get("kwh_max", 0) or 0), cc.get("kwh_action", "hold")
        return c.get("sess") or {}, float(c.get("kwh_max", 0) or 0), c.get("kwh_action", "hold")

    def amp_max(self, ch):
        chans = self.cfg().get("ch") or []
        return float(chans[ch].get("amp_max", 0) or 0) if ch < len(chans) else 0

    def slot_energy(self, s):
        return self.st["e"][s] if self.indep() else sum(self.st["e"])

    def next_seq(self):
        self.st["seq"] += 1
        return self.st["seq"]

    def event(self, e, **data):
        msg = {"t": "evt", "seq": self.next_seq(), "ts": int(time.time()), "tv": 1, "e": e, **data}
        with self.lock:
            self.buffer.append(msg)
        self.log("EVENT", e, data)

    # ------------------------------------------------------------ sessions
    def start_session(self, s, auto=False):
        self.st["sid_counter"] += 1
        _, mx, _ = self.slot_cfg(s)
        self.st["sess"][s] = {"sid": self.st["sid_counter"], "start": int(time.time()), "e0": self.slot_energy(s),
                              "status": "active", "over": False, "ended": False, "end_reason": None}
        ev = {"sid": self.st["sid_counter"], "auto": auto, "max": mx}
        if self.indep():
            ev["ch"] = s
        self.event("session_start", **ev)

    def end_session(self, s, reason):
        ss = self.st["sess"][s]
        if not ss.get("sid") or ss.get("ended"):
            return
        ss["ended"] = True
        ev = {"sid": ss["sid"], "reason": reason, "kwh": round(max(0, self.slot_energy(s) - ss["e0"]), 4)}
        if self.indep():
            ev["ch"] = s
        self.event("session_end", **ev)

    def stop_slot(self, s, why):
        ss = self.st["sess"][s]
        if not ss.get("sid") or ss.get("status") in ("ended", "expired"):
            return
        self.end_session(s, why)
        ss.update(status="ended", end_reason=why)

    def renew_slot(self, s):
        if self.disabled:
            return
        if self.st["sess"][s].get("sid"):
            self.end_session(s, "manual")
        self.start_session(s)

    def next_reset(self, s):
        sc, _, _ = self.slot_cfg(s)
        ss = self.st["sess"][s]
        if not ss.get("sid"):
            return None
        if sc.get("mode") == "duration":
            return ss["start"] + int(sc.get("dur_min", 1440)) * 60
        if sc.get("mode") == "daily":
            start = ss["start"]
            t = start - start % 86400 + int(sc.get("daily_utc_min", 0)) * 60
            return t + 86400 if t <= start else t
        return None

    # ------------------------------------------------------------ state
    def sess_json(self, s):
        ss = self.st["sess"][s]
        _, mx, _ = self.slot_cfg(s)
        o = {"sid": ss.get("sid", 0), "kwh": round(max(0, self.slot_energy(s) - ss.get("e0", 0)), 4), "max": mx,
             "status": ss.get("status", "active"), "ended": bool(ss.get("ended"))}
        if self.indep():
            o["ch"] = s
        if ss.get("start"):
            o["start"] = ss["start"]
        if ss.get("over"):
            o["over"] = True
        if ss.get("status") == "ended":
            o["end_reason"] = ss.get("end_reason") or ""
        nr = self.next_reset(s)
        if nr:
            o["next_reset"] = nr
        return o

    def state_msg(self):
        o = {"mode": "independent" if self.indep() else "shared",
             "relay": [{"i": i, "on": self.relay_on[i], "want": self.st["want"][i], "block": self.block[i],
                        "tripped": self.st["tripped"][i]} for i in range(self.n)],
             "tripped": any(self.st["tripped"]), "tamper": self.tamper, "ac": True}
        if self.disabled:
            o["disabled"] = True
        if self.indep():
            o["sessions"] = [self.sess_json(i) for i in range(self.slots())]
        else:
            o["sess"] = self.sess_json(0)
        return o

    def send_state(self):
        self.send({"t": "state", **self.state_msg()})

    # ------------------------------------------------------------ control loop (1 s)
    def control(self):
        last_report = 0
        while self.running:
            with self.lock:
                self.hw_tick()
                self.step()
                every = 1 if self.cfg().get("drdy") else float(self.cfg().get("read_s", 60))
                if time.time() - last_report >= every * self.args.speed:
                    last_report = time.time()
                    self.report()
            time.sleep(1 * self.args.speed)

    def step(self):
        v = random.uniform(228, 234)
        self.reading = []
        for i in range(self.n):
            w = self.load[i] * random.uniform(0.95, 1.05) if self.relay_on[i] else 0.0
            a = w / v
            self.st["e"][i] += w / 1000 / 3600 * self.args.energy_x
            self.reading.append({"i": i, "v": round(v, 1), "a": round(a, 3), "w": round(w, 1), "pf": 0.98, "hz": 50,
                                 "e": round(self.st["e"][i], 4)})
            lim = self.amp_max(i)
            if lim and a > lim and not self.st["tripped"][i]:
                self.st["tripped"][i] = True
                self.event("overcurrent", ch=i, val=round(a, 2), limit=lim)
        changed = False
        for s in range(self.slots()):
            ss = self.st["sess"][s]
            sc, mx, act = self.slot_cfg(s)
            kwh = self.slot_energy(s) - ss.get("e0", 0)
            if ss.get("sid") and ss.get("status") == "active" and mx > 0 and kwh >= mx:
                ev = {"kwh": round(kwh, 4), "action": act, "end": act != "warn"}
                if self.indep():
                    ev["ch"] = s
                if act == "warn":
                    if not ss.get("over"):
                        ss["over"] = True
                        self.event("quota", **ev)
                        changed = True
                else:
                    self.event("quota", **ev)
                    if act == "end":
                        self.end_session(s, "quota")
                    ss["status"] = "quota"
                    changed = True
            nr = self.next_reset(s)
            if ss.get("sid") and ss.get("status") not in ("expired", "ended") and nr and time.time() >= nr:
                self.end_session(s, "expired")
                if sc.get("auto"):
                    self.start_session(s, True)
                else:
                    ss["status"] = "expired"
                changed = True
        for i in range(self.n):
            st = self.st["sess"][self.slot_of(i)].get("status", "active")
            block = ("disabled" if self.disabled else "tripped" if self.st["tripped"][i] else
                     "no_session" if not self.st["sess"][self.slot_of(i)].get("sid") else
                     st if st in ("quota", "expired") else "ended" if st == "ended" else None)
            on = self.st["want"][i] and block is None
            if on != self.relay_on[i] or block != self.block[i]:
                self.relay_on[i], self.block[i] = on, block
                changed = True
        if changed:
            self.send_state()

    def report(self):
        if self.cfg().get("drdy"):  # DRDY data mode: ~10 measurement windows per second, packed
            t = time.time()
            frames = []
            for k in range(10):
                fr = [k * 100, self.reading[0]["v"]]
                for r in self.reading:
                    fr += [round(r["a"] * random.uniform(0.98, 1.02), 3), round(r["w"] * random.uniform(0.98, 1.02), 1), r["pf"]]
                frames.append(fr)
            item = {"seq": self.next_seq(), "ts": int(t), "tv": 1, "hz": 50, "e": [r["e"] for r in self.reading], "fr": frames}
        else:
            item = {"seq": self.next_seq(), "ts": int(time.time()), "tv": 1, "ch": self.reading}
        if self.indep():
            item["ss"] = [{"ch": s, "sid": self.st["sess"][s].get("sid", 0),
                           "kwh": round(max(0, self.slot_energy(s) - self.st["sess"][s].get("e0", 0)), 4)}
                          for s in range(self.slots())]
        else:
            item["s"] = {"sid": self.st["sess"][0].get("sid", 0),
                         "kwh": round(max(0, self.slot_energy(0) - self.st["sess"][0].get("e0", 0)), 4)}
        with self.lock:
            self.buffer.append({"t": "tel", "items": [item]})

    # ------------------------------------------------------------ server messages
    def handle(self, msg):
        t = msg.get("t")
        if t == "ack":
            last = msg.get("last_seq", 0)
            with self.lock:
                self.buffer = [m for m in self.buffer
                               if (max(i["seq"] for i in m["items"]) if m["t"] == "tel" else m["seq"]) > last]
        elif t == "cfg":
            with self.lock:
                old_indep = self.indep()
                new = msg.get("cfg") or {}
                if (new.get("mode") == "independent") != old_indep:  # mode change: sessions end
                    for s in range(self.slots()):
                        self.stop_slot(s, "mode")
                    self.st["cfg"] = new
                    for s in range(self.slots()):
                        if self.st["sess"][s].get("status", "active") == "active":
                            self.st["sess"][s].update(status="ended", end_reason="mode", ended=True)
                else:
                    self.st["cfg"] = new
                self.st["cfg_ver"] = msg.get("ver")
            self.send({"t": "cfg_ack", "ver": msg.get("ver"), "ok": True})
            self.send_state()
        elif t == "cmd":
            self.command(msg)

    def command(self, m):
        c, ch, ok, err = m.get("c"), m.get("ch"), True, None
        with self.lock:
            if c == "relay":
                i = ch or 0
                st = self.st["sess"][self.slot_of(i)].get("status", "active")
                if m.get("on") and (self.disabled or self.st["tripped"][i] or st != "active"):
                    ok, err = False, "tripped" if self.st["tripped"][i] else "session_expired"
                else:
                    self.st["want"][i] = bool(m.get("on"))
                    self.event("relay", ch=i, on=bool(m.get("on")), src="server")
            elif c == "renew":
                targets = [0] if not self.indep() else ([ch] if isinstance(ch, int) else range(self.slots()))
                for s in targets:
                    self.renew_slot(s)
            elif c == "end":
                targets = range(self.slots()) if not (self.indep() and isinstance(ch, int)) else [ch]
                for s in targets:
                    self.stop_slot(s, "stopped")
            elif c == "reset_trip":
                for i in range(self.n):
                    if (ch is None or ch == i) and self.st["tripped"][i]:
                        self.st["tripped"][i] = False
                        self.event("trip_reset", ch=i, src="server", by=m.get("by"))
            elif c == "send_now":
                self.report()
            elif c in ("disable", "enable"):
                self.disabled = c == "disable"
                self.st["disabled"] = self.disabled
                if self.disabled:
                    for s in range(self.slots()):
                        self.stop_slot(s, "disabled")
            elif c == "i2c_scan":
                res = {"found": sorted(self.bus), "rbamp": sorted(a for a in self.bus if 0x50 <= a <= 0x57)}
                self.send({"t": "cmd_ack", "ref": m.get("ref"), "ok": True, "res": res})
                return
            elif c == "rbamp_addr":
                old, new = m.get("from"), m.get("to")
                busy = any(x.get("sid") and not x.get("ended") for x in self.st["sess"])
                if busy:
                    ok, err = False, "busy"
                elif old not in self.bus or not 0x50 <= (old or 0) <= 0x57:
                    ok, err = False, "no_reply"
                elif new in self.bus:
                    ok, err = False, "addr_in_use"
                else:
                    self.bus.discard(old)
                    self.bus.add(new)
            elif c == "hw_profile":
                self.cmd_hw_profile(m)
                return
            elif c == "hw_cancel":
                if self.countdown:
                    self.hwp["state"] = "default" if self.countdown["was_default"] else "active"
                    self.countdown = None
                else:
                    ok, err = False, "nothing_pending"
            elif c == "get_state":
                pass
            elif c == "get_info":
                self.send({"t": "info", "fw": "sim-mc-0.7", "hw": "simulator", "sensor": "AC Power Sensor",
                           "kind": "ac", "sensor_detail": f"rbAmp UI{self.n}, {self.n} channel(s)", "relay_board": "XL9535 OK",
                           "uptime": int(time.time() - self.boot_t), "boot": self.st["boot"], "clock": int(time.time()),
                           "rtc": int(time.time()), "time_src": "server"})
            else:
                ok, err = False, "unknown_cmd"
            self.step()
        self.send({"t": "cmd_ack", "ref": m.get("ref"), "ok": ok, **({"err": err} if err else {})})
        self.send_state()

    # ------------------------------------------------------------ hardware profile (protocol.md §12)
    def ack(self, m, ok=True, err=None, **extra):
        self.send({"t": "cmd_ack", "ref": m.get("ref"), "ok": ok, **({"err": err} if err else {}), **extra})

    def cmd_hw_profile(self, m):
        prof, ver, mode = m.get("profile") or {}, m.get("ver"), m.get("mode")
        errors = validate_profile(prof)
        cur = self.hwp["profile"]
        same_struct = ([prof.get("boards"), prof.get("relays"), [(c["b"], c["i"], c["rb"], c["ro"]) for c in prof.get("ch", [])]] ==
                       [cur["boards"], cur["relays"], [(c["b"], c["i"], c["rb"], c["ro"]) for c in cur["ch"]]]) if not errors else False
        if errors:
            self.ack(m, False, "invalid", detail=errors[0][:60])
        elif mode == "live" and not same_struct:
            self.ack(m, False, "mode")
        elif mode == "live":
            self.hwp.update(ver=ver, profile=prof, state="active")
            self.log("HW profile", ver, "applied live (clamp models)")
            self.ack(m, True, res={"state": "live"})
        else:
            delay = float(m.get("delay_s", 60))
            self.countdown = {"due": time.time() + delay * self.args.speed, "msg": m,
                              "was_default": self.hwp["state"] == "default"}
            self.hwp["state"] = "pending"
            self.ack(m, True, res={"state": "pending", "in_s": int(delay)})
            self.event("hw_reboot_pending", ver=ver, in_s=int(delay))
            self.log("HW profile", ver, f"reboot in {delay:g} s")

    def hw_tick(self):
        """Reboot countdown, then the confirm check of the trial profile."""
        cd = self.countdown
        if not cd or time.time() < cd["due"]:
            return
        self.countdown = None
        m = cd["msg"]
        for s in range(self.slots()):
            self.stop_slot(s, "hw_change")
        self.hwp.update(prev_profile=self.hwp["profile"], prev_ver=self.hwp["ver"], prev_default=cd["was_default"],
                        profile=m["profile"], ver=m["ver"], state="trial")
        self.resize(len(m["profile"]["ch"]))
        self.log("RESTART with trial profile", m["ver"])
        self.reconnect()
        threading.Thread(target=self.hw_confirm, daemon=True).start()

    def hw_confirm(self):
        time.sleep(5 * self.args.speed)  # the firmware waits up to HW_CONFIRM_MS for every part to answer
        with self.lock:
            pr = self.hwp["profile"]
            need = list(pr["boards"]) + [r["addr"] for r in pr["relays"] if r["drv"] != "gpio"]
            missing = [a for a in need if a not in self.bus]
            if not missing:
                self.hwp["state"] = "active"
                self.event("hw_active", ver=self.hwp["ver"])
                return
            self.event("hw_rollback", ver=self.hwp["ver"], reason="no_reply", addr=missing[0])
            self.hwp.update(profile=self.hwp["prev_profile"], ver=self.hwp["prev_ver"],
                            state="default" if self.hwp.get("prev_default") else "active")
            self.resize(len(self.hwp["profile"]["ch"]))
            self.log("ROLLBACK, 0x%02X does not answer" % missing[0])
        self.reconnect()

    def reconnect(self):
        """The meter restarted: the server link is closed and opened again (new hello)."""
        ws = self.ws
        if ws is not None:
            try:
                ws.close()
            except Exception:
                pass

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
        with self.lock:
            pending = list(self.buffer)
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

    def ws_url(self):
        base = self.st["server"].replace("https://", "wss://").replace("http://", "ws://")
        return base + self.st.get("ws", "/ws/device")

    def net(self):
        while self.running:
            try:
                headers = {"Authorization": f"Bearer {self.st['device_id']}.{self.st['secret']}", "X-PM-Proto": "1"}
                self.ws = Client.connect(self.ws_url(), headers=headers)
                self.log("CONNECTED")
                with self.lock:
                    self.send({"t": "hello", "fw": "sim-mc-0.7", "proto": 1, "boot": self.st["boot"],
                               "up": int(time.time() - self.boot_t), "tv": 1, "ts": int(time.time()),
                               "cfg_ver": self.st.get("cfg_ver", 0), "caps": caps(self.n, self.hwp["profile"]),
                               "hwp": {"ver": self.hwp["ver"], "state": self.hwp["state"],
                                       "profile": self.hwp["profile"]}, "state": self.state_msg()})
                last_flush = 0
                while self.running:
                    if time.time() - last_flush > 3:
                        last_flush = time.time()
                        self.flush()
                    data = self.ws.receive(timeout=1)
                    if data:
                        self.handle(json.loads(data))
            except ConnectionClosed as e:
                self.log("DISCONNECTED", getattr(e, "reason", None))
            except Exception as e:  # connection refused etc.
                self.log("NET ERROR", e)
            self.ws = None
            time.sleep(3)

    def do(self, line):
        p = line.split()
        if not p:
            return
        with self.lock:
            if p[0] == "load" and len(p) == 3:
                self.load[int(p[1])] = float(p[2])
            elif p[0] == "trip" and len(p) == 2:
                i = int(p[1])
                self.st["tripped"][i] = True
                self.event("overcurrent", ch=i, val=99, limit=self.amp_max(i))
            elif p[0] in ("tamper_open", "tamper_close"):
                self.tamper = p[0] == "tamper_open"
                self.event(p[0])
            elif p[0] == "send":
                self.report()
            elif p[0] == "bus" and len(p) == 3 and p[1] in ("add", "del"):
                (self.bus.add if p[1] == "add" else self.bus.discard)(int(p[2], 0))
                self.log("I2C bus:", ", ".join("0x%02X" % a for a in sorted(self.bus)))
            elif p[0] == "quit":
                self.running = False

    def run(self):
        threading.Thread(target=self.control, daemon=True).start()
        threading.Thread(target=self.net, daemon=True).start()
        if sys.stdin.isatty():
            threading.Thread(target=lambda: [self.do(l) for l in sys.stdin], daemon=True).start()
        end = time.time() + self.args.duration if self.args.duration else None
        try:
            while self.running and (end is None or time.time() < end):
                time.sleep(0.5)
        finally:
            self.running = False
            save_state(self.args.state, self.st)
            if self.ws:
                try:
                    self.ws.close()
                except Exception:
                    pass


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--state", default="sim_meter_mc.json", help="state file (one per simulated meter)")
    sub = p.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("pair")
    a.add_argument("--server", required=True)
    a.add_argument("--code", required=True)
    a.add_argument("--device-id")
    a.add_argument("--channels", type=int, default=2, choices=range(1, 8))
    b = sub.add_parser("run")
    b.add_argument("--load", default="800", help="W per channel while its relay is ON, e.g. 600,1200")
    b.add_argument("--bus", help="I2C addresses that answer, e.g. 0x50,0x20 (default: those of the profile)")
    b.add_argument("--duration", type=float, default=0, help="stop after N seconds (0 = forever)")
    b.add_argument("--speed", type=float, default=1.0, help="<1 makes the loop faster")
    b.add_argument("--energy-x", type=float, default=1.0, help="multiply energy accumulation (test quotas)")
    args = p.parse_args()
    if args.cmd == "pair":
        cmd_pair(args)
    else:
        MeterMC(args).run()


if __name__ == "__main__":
    main()
