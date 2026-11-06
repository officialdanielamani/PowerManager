from flask import Blueprint, jsonify, render_template, request
from flask_login import current_user, login_required

from ..extensions import db
from ..models import Device, Incident, Notification, Session, now
from ..services import access, devconfig, hub, mesh, settings, tare as tare_svc

bp = Blueprint("main", __name__)


@bp.get("/healthz")
def healthz():
    return jsonify(ok=True)


def meter_summary(d, show_price=None):
    """Data for a dashboard card / live update (shared with meters.live)."""
    st = d.last_state or {}
    sess = st.get("sess") or {}
    reading = d.last_reading or {}
    chans = reading.get("ch") or []
    w_total = sum((c.get("w") or 0) for c in chans) if chans else None
    relays = st.get("relay") or []
    ongoing = [i.type for i in Incident.query.filter_by(device_fk=d.id, ended_at=None).all()]
    if d.disabled_at:
        status = "disabled"
    elif st.get("tripped"):
        status = "tripped"
    else:
        status = sess.get("status") or "unknown"
        if status == "ended":  # session ended by the voltage or tamper protection
            status = "ended_" + str(sess.get("end_reason") or "")
        if status == "active" and sess.get("over"):
            status = "over"  # kWh limit passed but "End session when kWh limit is reached" is off
    # Current session for the timer: start from the meter, end from our session record.
    cur = None
    if isinstance(sess.get("sid"), int):
        cur = Session.query.filter_by(device_fk=d.id, gen=d.pair_gen, sid=sess["sid"]).first()
    sess_start = sess.get("start") or (cur.started_at if cur else None)
    if show_price is None:
        show_price = access.can_price(current_user)
    currency, price = settings.price(d.kind)
    kwh_now = sess.get("kwh")
    if cur is not None and cur.price_per_kwh is not None:
        currency, price = cur.currency or "", cur.price_per_kwh
    cost = round(float(kwh_now) * price, 4) if show_price and price is not None and kwh_now is not None else None
    sess_end = cur.ended_at if cur and cur.ended_at else None
    # Tared view (own consumption subtracted); raw values stay in the normal fields.
    tare = tare_svc.offsets(d)
    chans_t = [tare_svc.reading(c, tare) for c in chans]
    w_t = sum((c.get("w") or 0) for c in chans_t) if chans_t else None
    kwh_t = None
    if tare and sess.get("kwh_t") is not None:
        kwh_t = round(float(sess["kwh_t"]), 4)  # the meter's own tared value (firmware 0.3+), used for its limit
    elif kwh_now is not None:
        secs = ((sess_end or now()) - sess_start) if sess_start else 0
        kwh_t = round(tare_svc.kwh(float(kwh_now), secs, tare), 4)
    cost_t = round(kwh_t * price, 4) if show_price and price is not None and kwh_t is not None else None
    out = {
        "tare": {"a": d.tare_a, "w": d.tare_w} if tare else None,
        "reading_tared": chans_t,
        "w_tared": round(w_t, 1) if w_t is not None else None,
        "kwh_tared": kwh_t,
        "cost_tared": settings.money(cost_t, currency) if cost_t is not None else None,
        "id": d.id,
        "device_id": d.device_id,
        "kind": d.kind,                 # "ac" / "dc"
        "sensor": d.sensor_name,
        "name": d.name,
        "info": d.info,
        "online": hub.is_online(d.id),
        "last_seen": settings.fmt_ts(d.last_seen_at),
        "relay_on": bool(relays and relays[0].get("on")),
        "relay_block": relays[0].get("block") if relays else None,
        "w": round(w_total, 1) if w_total is not None else None,
        "reading": chans,
        "reading_at": settings.fmt_ts(reading.get("ts")),
        "kwh": sess.get("kwh"),
        "kwh_max": round(float(sess["max"]), 4) if isinstance(sess.get("max"), (int, float)) else sess.get("max"),
        "status": status,
        "next_reset": settings.fmt_ts(sess.get("next_reset"), with_seconds=False) if sess.get("next_reset") else None,
        "next_reset_ts": sess.get("next_reset"),
        "sess_start_ts": sess_start,
        "sess_start": settings.fmt_ts(sess_start) if sess_start else None,
        "sess_end_ts": sess_end,
        "sess_mode": (d.cfg or {}).get("sess_mode", "none"),
        "server_time": now(),
        "reading_ts": reading.get("ts"),
        "reading_err": reading.get("err"),  # no channel data in the latest telemetry: no_reply / bad / no_ac
        "reading_ok_at": settings.fmt_ts(reading.get("last_ok_ts")) if reading.get("last_ok_ts") else None,
        "sess_ended": bool(sess_end) or sess.get("status") in ("expired", "ended") or bool(sess.get("ended")),
        "block": relays[0].get("block") if relays else None,
        "disabled": bool(d.disabled_at),
        "via": mesh.via_name(d),  # gateway name when connected through the mesh
        "cost": settings.money(cost, currency) if cost is not None else None,
        "synced": d.synced,
        "ongoing": [i for i in ongoing if i != "offline"],
        "session_desc": devconfig.describe_session(d.cfg or {}),
        "auto": bool((d.cfg or {}).get("auto")),
    }
    if d.is_mc:
        out.update(_mc_summary(d, out, show_price))
    return out


def _mc_summary(d, base, show_price):
    """Multi-channel meter: one entry per channel (reading, relay, and in independent mode its session)."""
    st = d.last_state or {}
    cfg = d.cfg or {}
    ccfg = cfg.get("ch") or []
    indep = d.mc_mode == "independent"
    readings = {c.get("i"): c for c in (d.last_reading or {}).get("ch") or []}
    relays = {r.get("i"): r for r in st.get("relay") or []}
    sessions = {s.get("ch"): s for s in st.get("sessions") or []} if indep else {}
    t = now()
    channels, totals, costs, maxes = [], 0.0, [], []
    for i in range(devconfig.channel_count(d)):
        rd, rl = readings.get(i) or {}, relays.get(i) or {}
        c = {"i": i, "name": d.channel_name(i), "label": (ccfg[i].get("name") if i < len(ccfg) else "") or "",
             "w": rd.get("w"), "a": rd.get("a"), "pf": rd.get("pf"), "e": rd.get("e"),
             "relay_on": bool(rl.get("on")), "relay_block": rl.get("block"), "tripped": bool(rl.get("tripped")),
             "amp_max": (ccfg[i].get("amp_max") if i < len(ccfg) else 0) or 0}
        if indep:
            ss = sessions.get(i) or {}
            rec = None
            if isinstance(ss.get("sid"), int) and ss["sid"] > 0:
                rec = Session.query.filter_by(device_fk=d.id, gen=d.pair_gen, sid=ss["sid"]).first()
            status = ss.get("status") or ("none" if not ss.get("sid") else "unknown")
            if status == "ended":
                status = "ended_" + str(ss.get("end_reason") or "")
            if status == "active" and ss.get("over"):
                status = "over"
            if not ss.get("sid"):
                status = "none"
            kwh = ss.get("kwh")
            cur, price = settings.price(d.kind)
            if rec is not None and rec.price_per_kwh is not None:
                cur, price = rec.currency or "", rec.price_per_kwh
            cost = round(float(kwh) * price, 4) if show_price and price is not None and kwh is not None else None
            start = ss.get("start") or (rec.started_at if rec else None)
            c.update({"sid": ss.get("sid"), "status": status, "kwh": kwh, "kwh_max": ss.get("max") or 0,
                      "cost": settings.money(cost, cur) if cost is not None else None,
                      "sess_start_ts": start, "sess_start": settings.fmt_ts(start) if start else None,
                      "sess_end_ts": rec.ended_at if rec and rec.ended_at else None,
                      "next_reset": settings.fmt_ts(ss.get("next_reset"), with_seconds=False) if ss.get("next_reset") else None,
                      "next_reset_ts": ss.get("next_reset"),
                      "sess_mode": (ccfg[i].get("sess_mode") if i < len(ccfg) else "none") or "none",
                      "sess_ended": status not in ("active", "over"),
                      "session_desc": devconfig.describe_session(ccfg[i] if i < len(ccfg) else {})})
            if kwh is not None:
                totals += float(kwh)
            if cost is not None:
                costs.append((cost, cur))
            maxes.append(float(ss.get("max") or 0))
        channels.append(c)
    out = {"mc": True, "mode": d.mc_mode, "channels": channels, "drdy": bool(cfg.get("drdy")),
           "relay_on": any(c["relay_on"] for c in channels)}
    if indep:
        statuses = {c["status"] for c in channels}
        out.update({
            "status": "active" if statuses <= {"active"} else ("independent_mixed" if "active" in statuses else
                                                              next(iter(statuses)) if len(statuses) == 1 else "independent_mixed"),
            "kwh": round(totals, 4),
            "kwh_max": round(sum(maxes), 4) if maxes and all(m > 0 for m in maxes) else 0,
            "cost": settings.money(sum(c for c, _ in costs), costs[0][1]) if costs else None,
            "sess_start": None, "next_reset": None, "session_desc": "Per channel (independent)",
        })
    return out


@bp.get("/")
@login_required
def dashboard():
    devices = access.visible_devices(current_user).all()
    removed = access.visible_devices(current_user, removed=True).all()
    return render_template("dashboard.html", meters=[meter_summary(d) for d in devices],
                           removed=removed)


@bp.get("/api/ui/meters")
@login_required
def meters_json():
    devices = access.visible_devices(current_user).all()
    return jsonify(meters=[meter_summary(d) for d in devices])


@bp.get("/notifications")
@login_required
def notifications():
    page = request.args.get("page", 1, type=int)
    q = Notification.query.filter_by(user_fk=current_user.id).order_by(Notification.ts.desc())
    pagination = q.paginate(page=page, per_page=50, error_out=False)
    names = {d.id: d.name for d in Device.query.all()}
    return render_template("notifications.html", pagination=pagination, names=names)


@bp.post("/notifications/delete")
@login_required
def notifications_delete():
    """Delete selected (ids) or all of the current user's notifications."""
    body = request.get_json(silent=True) or {}
    q = Notification.query.filter_by(user_fk=current_user.id)
    if not body.get("all"):
        ids = [int(i) for i in body.get("ids", []) if str(i).isdigit()]
        q = q.filter(Notification.id.in_(ids or [-1]))
    n = q.delete(synchronize_session=False)
    db.session.commit()
    return jsonify(ok=True, deleted=n, message=f"Deleted {n} notification(s)")


@bp.post("/notifications/read-all")
@login_required
def notifications_read_all():
    Notification.query.filter_by(user_fk=current_user.id, read=False).update({"read": True})
    db.session.commit()
    return jsonify(ok=True)


@bp.get("/firmware")
@login_required
def firmware():
    if not current_user.is_admin:
        from flask import abort
        abort(403)
    return render_template("firmware.html")
