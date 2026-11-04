"""Handle messages from a connected meter (docs/protocol.md §2)."""
import logging

from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from ..extensions import db
from ..models import Device, Event, Incident, Measurement, RollupDirty, Session, now
from . import commands, devconfig, incidents, notify, settings

log = logging.getLogger(__name__)

MAX_FUTURE_S = 300
FIELDS = ("v", "a", "w", "pf", "hz", "e")
# Physically possible ranges; anything outside is stored as "no value" (bad sensor read or
# a misbehaving client). Generous on purpose.
LIMITS = {"v": (0, 400), "a": (0, 200), "w": (0, 80000), "pf": (0, 1.0001), "hz": (0, 70),
          "e": (0, 10_000_000)}


# ---------------------------------------------------------------- helpers

def _f(value):
    try:
        return None if value is None else float(value)
    except (TypeError, ValueError):
        return None


def resolve_ts(conn, item):
    """Return (ts, estimated) for an item that may have been recorded without valid time."""
    t = now()
    try:
        ts = int(item.get("ts", 0))
    except (TypeError, ValueError):
        return t, True
    if item.get("tv", 1):
        if ts <= 0 or ts > t + MAX_FUTURE_S:
            return t, True
        return ts, False
    # ts holds uptime seconds of boot `boot`
    if conn is not None and conn.boot_epoch is not None and item.get("boot") == conn.boot:
        return min(conn.boot_epoch + ts, t), True
    return t, True


def _mark_dirty(device_pk, ch, ts):
    hour = ts - ts % 3600
    day = ts - ts % 86400
    stmt = sqlite_insert(RollupDirty).values([
        {"device_fk": device_pk, "ch": ch, "period": "h", "bucket_start": hour},
        {"device_fk": device_pk, "ch": ch, "period": "d", "bucket_start": day},
    ]).on_conflict_do_nothing()
    db.session.execute(stmt)


def server_event(device, etype, data=None, ts=None):
    ev = Event(device_fk=device.id, gen=device.pair_gen, seq=None, ts=ts or now(), type=etype,
               data=data or {})
    db.session.add(ev)
    db.session.flush()
    return ev


def _bump_seq(device, seq):
    try:
        seq = int(seq)
    except (TypeError, ValueError):
        return
    if seq > device.last_seq:
        device.last_seq = seq


# ---------------------------------------------------------------- connect / disconnect

def on_connect(device, conn):
    t = now()
    if device.disabled_at:  # read-only meter: no incidents
        device.online, device.last_seen_at = True, t
        db.session.commit()
        return
    was_offline = incidents.find_open(device.id, "offline") is not None
    device.online = True
    device.last_seen_at = t
    ev = server_event(device, "reconnected" if was_offline else "connected", ts=t)
    inc = incidents.close_incident(device.id, "offline", t, event=ev)
    if inc is not None:
        notify.notify(device, "reconnected",
                      f"Reconnected after {incidents.fmt_duration(inc.duration_s)} offline")
    db.session.commit()


def on_disconnect(device, reason="connection_lost"):
    t = now()
    device.online = False
    if device.removed_at or device.disabled_at:
        db.session.commit()
        return
    ev = server_event(device, "disconnected", {"reason": reason}, ts=t)
    # A timeout means the device went silent after its last message; a closed socket is "now".
    start = (device.last_seen_at or t) if reason == "ping_timeout" else t
    incidents.open_incident(device.id, "offline", start, event=ev, detail={"cause": reason})
    merge_power_into_offline(device)
    db.session.commit()


MERGE_WINDOW_S = 60


def merge_power_into_offline(device):
    """No Power and Offline starting within a minute of each other = the whole meter lost power
    (its UPS ran out). Close the No Power incident; the Offline incident covers it."""
    off = incidents.find_open(device.id, "offline")
    pwr = incidents.find_open(device.id, "power")
    if off is None or pwr is None or abs(pwr.started_at - off.started_at) > MERGE_WINDOW_S:
        return
    incidents.close_incident(device.id, "power", max(off.started_at, pwr.started_at),
                             detail={"closed_by": "offline", "note": "meter lost power (offline at the same time)"})
    off.detail = {**(off.detail or {}), "power_lost": True}


# ---------------------------------------------------------------- dispatcher

def handle(conn, device, msg):
    """Process one message. Returns nothing; replies go through conn.send()."""
    t = msg.get("t")
    device.last_seen_at = now()
    if device.disabled_at and t in ("tel", "evt"):
        # Disabled = read-only: acknowledge so the meter empties its buffer, but store nothing.
        seqs = [i.get("seq") for i in (msg.get("items") or []) if isinstance(i, dict)] if t == "tel" else [msg.get("seq")]
        for sq in seqs:
            _bump_seq(device, sq)
        conn.send({"t": "ack", "last_seq": device.last_seq, "time": now()})
        db.session.commit()
        return
    handler = HANDLERS.get(t)
    if handler is None:
        return  # unknown types are ignored (forward compatibility)
    handler(conn, device, msg)
    db.session.commit()


def h_ping(conn, device, msg):
    conn.send({"t": "pong", "time": now()})


def h_hello(conn, device, msg):
    try:
        up = int(msg.get("up", 0))
    except (TypeError, ValueError):
        up = 0
    conn.boot = msg.get("boot")
    conn.boot_epoch = now() - up
    device.fw = str(msg.get("fw", device.fw or ""))[:32]
    try:
        device.proto = int(msg.get("proto", 1))
    except (TypeError, ValueError):
        device.proto = 1
    if isinstance(msg.get("caps"), dict):
        caps = dict(device.caps or {})
        caps.update(msg["caps"])
        device.caps = caps
    state = msg.get("state")
    if isinstance(state, dict):
        _apply_state(device, state)
        _reconcile_incidents(device, state)
    try:
        dev_ver = int(msg.get("cfg_ver", 0))
    except (TypeError, ValueError):
        dev_ver = 0
    if dev_ver == device.cfg_ver_wanted:
        device.cfg_ver_applied = dev_ver
    conn.send({"t": "welcome", "time": now(), "cfg_ver": device.cfg_ver_wanted,
               "last_seq": device.last_seq})
    if not device.link_key and getattr(conn, "direct", True):
        import secrets
        device.link_key = secrets.token_hex(16)  # mesh signing key for meters paired before 0.4
        device.link_up = 0
    if getattr(conn, "direct", True) and device.link_key:
        conn.send({"t": "link_key", "key": device.link_key})  # meter stores it (needed for mesh)
    if dev_ver != device.cfg_ver_wanted:
        conn.send({"t": "cfg", **devconfig.to_device(device.cfg, device.cfg_ver_wanted)})
    commands.flush_pending(device, conn)


def h_tel(conn, device, msg):
    items = msg.get("items") or []
    newest = None
    for item in items[:200]:
        if not isinstance(item, dict) or "seq" not in item:
            continue
        ts, est = resolve_ts(conn, item)
        try:
            seq = int(item["seq"])
        except (TypeError, ValueError):
            continue
        rows = []
        for ch in item.get("ch") or []:
            if not isinstance(ch, dict):
                continue
            try:
                idx = int(ch.get("i", 0))
            except (TypeError, ValueError):
                continue
            row = {"device_fk": device.id, "gen": device.pair_gen, "ch": idx, "seq": seq, "ts": ts, "time_estimated": est}
            for f in FIELDS:
                val = _f(ch.get(f))
                lo, hi = LIMITS[f]
                row[f] = val if val is not None and lo <= val <= hi else None
            rows.append(row)
        if rows:
            db.session.execute(sqlite_insert(Measurement).values(rows).on_conflict_do_nothing())
            for row in rows:
                _mark_dirty(device.id, row["ch"], ts)
        _bump_seq(device, seq)
        if newest is None or ts >= newest["ts"]:
            newest = {"ts": ts, "est": est, "ch": [
                {"i": r["ch"], **{f: r[f] for f in FIELDS}} for r in rows]}
    if newest is not None and newest["ch"] and newest["ts"] >= (device.last_reading or {}).get("ts", 0):
        device.last_reading = newest
    _session_progress(device, items)
    conn.send({"t": "ack", "last_seq": device.last_seq, "time": now()})


def _session_progress(device, items):
    """Telemetry items carry {"s": {"sid", "kwh"}}: keep "session used" current between state messages."""
    latest = None
    for item in items[:200]:
        if isinstance(item, dict) and isinstance(item.get("s"), dict) and isinstance(item["s"].get("sid"), int):
            latest = item["s"]
    if latest is None:
        return
    kwh = _f(latest.get("kwh"))
    st = dict(device.last_state or {})
    sess = dict(st.get("sess") or {})
    if kwh is None or sess.get("sid") not in (None, latest["sid"]):
        return  # older session than the one we know: ignore
    sess["sid"], sess["kwh"] = latest["sid"], kwh
    if latest.get("kwh_t") is not None:
        sess["kwh_t"] = _f(latest.get("kwh_t"))
    st["sess"] = sess
    device.last_state = st
    s = Session.query.filter_by(device_fk=device.id, gen=device.pair_gen, sid=latest["sid"]).first()
    if s is not None and s.ended_at is None:
        s.kwh_used = kwh


def h_evt(conn, device, msg):
    if "seq" not in msg:
        return
    try:
        seq = int(msg["seq"])
    except (TypeError, ValueError):
        return
    exists = Event.query.filter_by(device_fk=device.id, gen=device.pair_gen, seq=seq).first()
    if exists is None:
        ts, est = resolve_ts(conn, msg)
        data = {k: v for k, v in msg.items() if k not in ("t", "id", "seq", "ts", "tv", "e", "ch", "boot")}
        ch = msg.get("ch")
        ev = Event(device_fk=device.id, gen=device.pair_gen, seq=seq, ts=ts, type=str(msg.get("e", "unknown"))[:32],
                   ch=ch if isinstance(ch, int) else None, data=data, time_estimated=est)
        db.session.add(ev)
        db.session.flush()
        process_event(device, ev)
    _bump_seq(device, seq)
    conn.send({"t": "ack", "last_seq": device.last_seq, "time": now()})


def h_state(conn, device, msg):
    _apply_state(device, msg)


def h_cfg_ack(conn, device, msg):
    try:
        ver = int(msg.get("ver", 0))
    except (TypeError, ValueError):
        return
    if msg.get("ok"):
        if ver >= device.cfg_ver_applied:
            device.cfg_ver_applied = ver
        device.cfg_error = None
        server_event(device, "config_applied", {"ver": ver})
    else:
        device.cfg_error = str(msg.get("err", "rejected"))[:200]
        server_event(device, "config_rejected", {"ver": ver, "err": device.cfg_error})


def h_info(conn, device, msg):
    """Answer to "Get meter info"."""
    keep = ("ip", "ssid", "rssi", "mac", "heap", "heap_min", "psram", "psram_free", "fw", "hw", "hw_info",
            "bat_v", "bat_pct", "uptime", "boot", "core", "chip", "flash", "outbox", "time_src",
            "link", "gateway", "mesh_ch", "leaves", "stack")
    device.last_info = {k: msg[k] for k in keep if k in msg}
    device.last_info_at = now()


def h_cmd_ack(conn, device, msg):
    commands.ack(device, msg.get("ref"), msg.get("ok"), msg.get("err"))


HANDLERS = {
    "ping": h_ping,
    "hello": h_hello,
    "tel": h_tel,
    "evt": h_evt,
    "state": h_state,
    "cfg_ack": h_cfg_ack,
    "cmd_ack": h_cmd_ack,
    "info": h_info,
}


# ---------------------------------------------------------------- state / events

def _apply_state(device, state):
    clean = {k: state[k] for k in ("relay", "tripped", "tamper", "ac", "sess", "volt_out") if k in state}
    merged = dict(device.last_state or {})
    merged.update(clean)
    merged["at"] = now()
    device.last_state = merged
    sess = clean.get("sess")
    if isinstance(sess, dict) and isinstance(sess.get("sid"), int):
        s = _session(device, sess["sid"])
        if sess.get("start"):
            s.started_at = s.started_at or int(sess["start"])
        s.kwh_used = _f(sess.get("kwh")) or 0.0
        s.kwh_max = _f(sess.get("max")) or 0.0


def _session(device, sid):
    s = Session.query.filter_by(device_fk=device.id, gen=device.pair_gen, sid=sid).first()
    if s is None:
        s = Session(device_fk=device.id, gen=device.pair_gen, sid=sid)
        db.session.add(s)
        db.session.flush()
    return s


def _reconcile_incidents(device, state):
    """Close incidents whose end event was lost; open ones whose start was missed."""
    t = now()
    checks = []
    if "tamper" in state:
        checks.append(("tamper", bool(state["tamper"])))
    if "ac" in state:
        checks.append(("power", state["ac"] is False))
    if "tripped" in state:
        checks.append(("trip", bool(state["tripped"])))
    for itype, active in checks:
        open_inc = incidents.find_open(device.id, itype)
        if active and open_inc is None:
            incidents.open_incident(device.id, itype, t, detail={"opened_by": "state_sync"})
        elif not active and open_inc is not None:
            incidents.close_incident(device.id, itype, t, detail={"closed_by": "state_sync"})


def process_event(device, ev):
    d = ev.data or {}
    est = ev.time_estimated
    e = ev.type
    if e == "overcurrent":
        incidents.open_incident(device.id, "trip", ev.ts, ch=None, event=ev, estimated=est,
                                detail={"a": d.get("val"), "limit": d.get("limit"), "ch": ev.ch})
        notify.notify(device, "max_amp", f"Over-current trip: {d.get('val')} A")
    elif e == "trip_reset":
        incidents.close_incident(device.id, "trip", ev.ts, event=ev, estimated=est,
                                 detail={"reset_by": d.get("by"), "reset_src": d.get("src")})
    elif e == "tamper_open":
        act = d.get("action")  # only when Tamper Protection is enabled on the meter
        incidents.open_incident(device.id, "tamper", ev.ts, event=ev, estimated=est,
                                detail={"action": act} if act else None)
        what = {"end": ": session ended", "hold": ": relay off until closed"}.get(act, "")
        notify.notify(device, "panel_open", f"Panel opened (tamper switch){what}")
    elif e == "tamper_close":
        incidents.close_incident(device.id, "tamper", ev.ts, event=ev, estimated=est)
    elif e == "voltage_out":
        def num(x):
            return f"{float(x):g}" if x not in (None, "", 0, 0.0) else "–"
        lim = f"{num(d.get('min'))}–{num(d.get('max'))} V"
        incidents.open_incident(device.id, "voltage", ev.ts, event=ev, estimated=est,
                                detail={"v": d.get("v"), "dir": d.get("dir"), "range": lim, "action": d.get("action")})
        what = {"end": "session ended", "hold": "relay off until back in range"}.get(d.get("action"), "power kept on")
        notify.notify(device, "voltage_range",
                      f"Voltage {'too low' if d.get('dir') == 'low' else 'too high'}: {d.get('v')} V "
                      f"(range {lim}): {what}")
    elif e == "voltage_ok":
        incidents.close_incident(device.id, "voltage", ev.ts, event=ev, estimated=est, detail={"v_back": d.get("v")})
    elif e == "ac_lost":
        incidents.open_incident(device.id, "power", ev.ts, event=ev, estimated=est)
        merge_power_into_offline(device)
        notify.notify(device, "no_voltage", "No voltage (AC power lost)")
    elif e == "ac_restored":
        incidents.close_incident(device.id, "power", ev.ts, event=ev, estimated=est)
    elif e == "quota":
        # action: end (power off, session ends now) | hold (power off, session ends at its time
        # limit) | warn (power stays on). Firmware 0.1.x sends only "end": true/false.
        action = d.get("action") or ("end" if d.get("end", True) else "warn")
        kwh = d.get("kwh", "?")
        if action == "end":
            # Close the session record here as well, so it ends at the limit even if the
            # meter's session_end arrives late or never (older firmware only sent it on Renew).
            sid = ((device.last_state or {}).get("sess") or {}).get("sid")
            if isinstance(sid, int):
                s = _session(device, sid)
                if s.ended_at is None:
                    s.ended_at, s.end_reason = ev.ts, "quota"  # kWh used: from session_end (raw)
            notify.notify(device, "kwh_exceeded", f"Session kWh limit reached ({kwh} kWh): power off, session ended")
        elif action == "hold":
            notify.notify(device, "kwh_exceeded",
                          f"Session kWh limit reached ({kwh} kWh): power off until the session ends or is renewed")
        else:
            notify.notify(device, "kwh_exceeded", f"Session kWh limit exceeded ({kwh} kWh): power kept on")
    elif e == "session_start":
        sid = d.get("sid")
        if isinstance(sid, int):
            s = _session(device, sid)
            s.started_at = ev.ts
            s.auto = bool(d.get("auto"))
            cur, price = settings.price()
            if price is not None and s.price_per_kwh is None:
                s.price_per_kwh, s.currency = price, cur
            s.kwh_max = _f(d.get("max")) or s.kwh_max
        if sid != 1:  # the very first session after pairing is not a "renew"
            notify.notify(device, "session_renew",
                          "Session auto-renewed" if d.get("auto") else "Session renewed")
    elif e == "session_end":
        sid = d.get("sid")
        if isinstance(sid, int):
            s = _session(device, sid)
            if s.ended_at is None:  # keep the first end (e.g. the kWh limit), not a later Renew
                s.ended_at = ev.ts
                s.end_reason = str(d.get("reason", ""))[:10] or None
            if d.get("kwh") is not None:  # always the raw kWh
                s.kwh_used = _f(d.get("kwh")) or 0.0
    elif e == "server_back":
        last_off = (Incident.query
                    .filter_by(device_fk=device.id, type="offline")
                    .order_by(Incident.id.desc()).first())
        if last_off is not None:
            detail = dict(last_off.detail or {})
            detail["device_back_at"] = ev.ts
            if d.get("since"):
                detail["device_lost_at"] = d.get("since")
            last_off.detail = detail


def mark_all_offline_on_startup():
    """Server just started: devices marked online lost their connection while we were down."""
    t = now()
    for device in Device.query.filter(Device.online.is_(True)).all():
        device.online = False
        ev = server_event(device, "disconnected", {"reason": "server_down"}, ts=t)
        incidents.open_incident(device.id, "offline", device.last_seen_at or t, event=ev,
                                detail={"cause": "server_down"})
    db.session.commit()
