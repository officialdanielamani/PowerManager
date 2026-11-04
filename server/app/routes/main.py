from flask import Blueprint, jsonify, render_template, request
from flask_login import current_user, login_required

from ..extensions import db
from ..models import Device, Incident, Notification, Session, now
from ..services import access, devconfig, hub, settings

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
    if st.get("tripped"):
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
    currency, price = settings.price()
    kwh_now = sess.get("kwh")
    if cur is not None and cur.price_per_kwh is not None:
        currency, price = cur.currency or "", cur.price_per_kwh
    cost = round(float(kwh_now) * price, 4) if show_price and price is not None and kwh_now is not None else None
    sess_end = cur.ended_at if cur and cur.ended_at else None
    return {
        "id": d.id,
        "device_id": d.device_id,
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
        "kwh_max": sess.get("max"),
        "status": status,
        "next_reset": settings.fmt_ts(sess.get("next_reset"), with_seconds=False) if sess.get("next_reset") else None,
        "next_reset_ts": sess.get("next_reset"),
        "sess_start_ts": sess_start,
        "sess_start": settings.fmt_ts(sess_start) if sess_start else None,
        "sess_end_ts": sess_end,
        "sess_mode": (d.cfg or {}).get("sess_mode", "none"),
        "server_time": now(),
        "reading_ts": reading.get("ts"),
        "sess_ended": bool(sess_end) or sess.get("status") in ("expired", "ended") or bool(sess.get("ended")),
        "block": relays[0].get("block") if relays else None,
        "cost": settings.money(cost, currency) if cost is not None else None,
        "synced": d.synced,
        "ongoing": [i for i in ongoing if i != "offline"],
        "session_desc": devconfig.describe_session(d.cfg or {}),
        "auto": bool((d.cfg or {}).get("auto")),
    }


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
