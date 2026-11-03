from flask import Blueprint, jsonify, render_template, request
from flask_login import login_required

from ..extensions import db
from ..models import Device, Incident, Notification
from ..services import devconfig, hub, settings

bp = Blueprint("main", __name__)


@bp.get("/healthz")
def healthz():
    return jsonify(ok=True)


def meter_summary(d):
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
        "synced": d.synced,
        "ongoing": [i for i in ongoing if i != "offline"],
        "session_desc": devconfig.describe_session(d.cfg or {}),
        "auto": bool((d.cfg or {}).get("auto")),
    }


@bp.get("/")
@login_required
def dashboard():
    devices = Device.query.filter(Device.removed_at.is_(None)).order_by(Device.name).all()
    removed = Device.query.filter(Device.removed_at.isnot(None)).order_by(Device.name).all()
    return render_template("dashboard.html", meters=[meter_summary(d) for d in devices],
                           removed=removed)


@bp.get("/api/ui/meters")
@login_required
def meters_json():
    devices = Device.query.filter(Device.removed_at.is_(None)).order_by(Device.name).all()
    return jsonify(meters=[meter_summary(d) for d in devices])


@bp.get("/notifications")
@login_required
def notifications():
    page = request.args.get("page", 1, type=int)
    q = Notification.query.order_by(Notification.ts.desc())
    pagination = q.paginate(page=page, per_page=50, error_out=False)
    names = {d.id: d.name for d in Device.query.all()}
    return render_template("notifications.html", pagination=pagination, names=names)


@bp.post("/notifications/read-all")
@login_required
def notifications_read_all():
    Notification.query.filter_by(read=False).update({"read": True})
    db.session.commit()
    return jsonify(ok=True)
