import csv
import io
import logging
import json
import secrets

import qrcode
import qrcode.image.svg
from flask import (Blueprint, Response, abort, flash, jsonify, redirect, render_template, request,
                   url_for)
from flask_login import current_user, login_required

from ..extensions import db
from ..models import (Command, Device, Event, Incident, Measurement, MeterViewer, Notification,
                      NotificationRule, User,
                      PairingCode, Rollup, RollupDirty, Session, now)
from ..services import access, commands, devconfig, history, hub, incidents, ingest, lifecycle, settings, validation
from ..services import tare as tare_svc
from .decorators import admin_required
from .device_api import ensure_device_defaults
from .main import meter_summary

bp = Blueprint("meters", __name__, url_prefix="/meters")
log = logging.getLogger(__name__)

PAIRING_TTL_S = 600
CODE_ALPHABET = "23456789ABCDEFGHJKMNPQRSTUVWXYZ"


def _device(pk, allow_removed=False):
    """Admin pages: any meter. Shared pages use access.device_for_current_user()."""
    d = db.session.get(Device, pk)
    if d is None or (d.removed_at and not allow_removed):
        abort(404)
    return d


# ------------------------------------------------------------------ add / pairing

def _qr_svg(payload):
    img = qrcode.make(payload, image_factory=qrcode.image.svg.SvgPathImage, box_size=12, border=2)
    return img.to_string(encoding="unicode")


@bp.get("/add")
@admin_required
def add():
    PairingCode.query.filter(PairingCode.expires_at < now() - 86400).delete()
    code = "".join(secrets.choice(CODE_ALPHABET) for _ in range(8))
    pc = PairingCode(code=code, expires_at=now() + PAIRING_TTL_S, created_by=current_user.id)
    db.session.add(pc)
    db.session.commit()
    host, port, tls, configured = settings.public_server(request)
    payload = json.dumps({"pm": 1, "h": host, "p": port, "s": 1 if tls else 0, "c": code},
                         separators=(",", ":"))
    return render_template("meters/add.html", code=code, code_display=f"{code[:4]}-{code[4:]}",
                           host=host, port=port, tls=tls, configured=configured,
                           qr_svg=_qr_svg(payload), payload=payload, ttl=PAIRING_TTL_S,
                           expires_at=pc.expires_at)


@bp.get("/pairing/<code>/status")
@admin_required
def pairing_status(code):
    pc = db.session.get(PairingCode, code)
    if pc is None:
        return jsonify(status="unknown")
    if pc.used_at and pc.device_fk:
        return jsonify(status="paired", url=url_for("meters.configure", pk=pc.device_fk, new=1))
    if pc.expires_at < now():
        return jsonify(status="expired")
    return jsonify(status="pending", remaining=pc.expires_at - now())


# ------------------------------------------------------------------ configure

def _push_config(device):
    msg = {"t": "cfg", **devconfig.to_device(device.cfg, device.cfg_ver_wanted)}
    return hub.send(device.id, msg) is not None


def _rules(device, user):
    rules = {r.type: r for r in NotificationRule.query.filter_by(device_fk=device.id, user_fk=user.id).all()}
    out = []
    for ntype, label in devconfig.NOTIFY_TYPES:
        r = rules.get(ntype)
        out.append({"type": ntype, "label": label, "enabled": bool(r and r.enabled),
                    "param": (r.param if r and r.param else devconfig.DISCONNECTED_DEFAULT_MIN)})
    return out


def _viewers():
    return User.query.filter_by(role="viewer").order_by(User.username).all()


def _set_viewers(device, user_ids):
    MeterViewer.query.filter_by(device_fk=device.id).delete()
    valid = {u.id for u in _viewers()}
    for uid in user_ids:
        if uid in valid:
            db.session.add(MeterViewer(device_fk=device.id, user_fk=uid))
    # Notification rules of users who can no longer see the meter are dropped.
    for r in NotificationRule.query.filter_by(device_fk=device.id).all():
        u = db.session.get(User, r.user_fk)
        if u is not None and u.role == "viewer" and not u.view_all and r.user_fk not in user_ids:
            db.session.delete(r)


@bp.route("/<int:pk>/configure", methods=["GET", "POST"])
@admin_required
def configure(pk):
    device = _device(pk)
    if device.disabled_at:
        flash("This meter is disabled (read-only). Enable it to change its configuration.", "warning")
        return redirect(url_for("meters.info", pk=pk))
    errors = []
    waiting = False
    if request.method == "POST":
        form = request.form
        name, err = validation.text(form.get("name"), "Nick Name", 1, 40)
        errors += [err] if err else []
        info, err = validation.text(form.get("info"), "Info", 0, 200)
        errors += [err] if err else []
        cfg, cfg_errors = devconfig.parse_form(form, device)
        errors += cfg_errors
        retention = {}
        for key, label in (("data", "Data"), ("events", "Historical events")):
            raw = (form.get(f"{key}_autodel_days") or "120").strip()
            if not raw.isdigit() or not 1 <= int(raw) <= 3650:
                errors.append(f"{label} auto-delete: days must be 1–3650")
            else:
                retention[key] = (form.get(f"{key}_autodel") == "on", int(raw))
        if not errors:
            device.tare_a, device.tare_w = cfg["tare_a"], cfg["tare_w"]  # mirror (tare is part of cfg)
            device.data_autodel, device.data_autodel_days = retention["data"]
            device.events_autodel, device.events_autodel_days = retention["events"]
            device.name, device.info = name, info
            config_changed = cfg != device.cfg
            device.cfg = cfg
            _set_viewers(device, {int(x) for x in form.getlist("viewers") if x.isdigit()})
            if config_changed or not device.synced:
                if config_changed:
                    device.cfg_ver_wanted += 1
                device.cfg_error = None
                db.session.commit()
                _push_config(device)
                waiting = True
            else:
                db.session.commit()
                flash("Saved.", "success")
                return redirect(url_for("meters.info", pk=device.id))
    cfg = device.cfg or devconfig.default_cfg(devconfig.channel_count(device))
    dur = int(cfg.get("dur_min", 1440))
    return render_template("meters/configure.html", d=device, cfg=cfg, errors=errors,
                           waiting=waiting, online=hub.is_online(device.id), kwh_actions=devconfig.KWH_ACTIONS,
                           protect_actions=devconfig.PROTECT_ACTIONS, kwh_action_of=devconfig.kwh_action,
                           viewers=_viewers(), linked={v.user_fk for v in MeterViewer.query.filter_by(device_fk=device.id)},
                           dur_d=dur // 1440, dur_h=(dur % 1440) // 60, dur_m=dur % 60,
                           new=request.args.get("new") == "1", amp_rating=devconfig.amp_rating(device),
                           form=request.form if errors else None)


@bp.route("/<int:pk>/notifications", methods=["GET", "POST"])
@login_required
def notifications(pk):
    """Each account (admin or viewer) picks its own notifications for this meter."""
    device = access.device_for_current_user(pk)
    if request.method == "POST":
        form = request.form
        disc_min = (form.get("notify_disconnected_min") or "5").strip()
        if not disc_min.isdigit() or not 1 <= int(disc_min) <= 1440:
            flash("Disconnected minutes must be 1–1440", "danger")
            return redirect(url_for("meters.notifications", pk=pk))
        for ntype, _label in devconfig.NOTIFY_TYPES:
            r = db.session.get(NotificationRule, (device.id, current_user.id, ntype))
            if r is None:
                r = NotificationRule(device_fk=device.id, user_fk=current_user.id, type=ntype)
                db.session.add(r)
            r.enabled = form.get(f"notify_{ntype}") == "on"
            if ntype == "disconnected":
                r.param = int(disc_min)
        db.session.commit()
        flash("Your notification options for this meter were saved.", "success")
        return redirect(url_for("meters.notifications", pk=pk))
    return render_template("meters/notifications.html", d=device, rules=_rules(device, current_user),
                           telegram=settings.get("telegram_enabled") == "1",
                           has_chat=bool(current_user.telegram_chat_id))


@bp.get("/<int:pk>/sync-status")
@login_required
def sync_status(pk):
    d = access.device_for_current_user(pk)
    return jsonify(synced=d.synced, online=hub.is_online(d.id), error=d.cfg_error,
                   wanted=d.cfg_ver_wanted, applied=d.cfg_ver_applied)


@bp.post("/<int:pk>/resync")
@admin_required
def resync(pk):
    """Re-paired hardware that was unsynced before: continue its history, or start as a new meter."""
    device = _device(pk)
    if not device.resync_pending:
        return redirect(url_for("meters.configure", pk=pk))
    if request.form.get("choice") == "new":
        archived = lifecycle.resync_as_new(device)
        flash(f"Started as a new meter. The old history is kept as '{archived.name}' under unsynced meters.", "success")
    else:
        device.resync_pending = False
        db.session.commit()
        flash(f"Continuing as '{device.name}' with its history.", "success")
    return redirect(url_for("meters.configure", pk=pk, new=1))


@bp.get("/<int:pk>/device-info")
@admin_required
def device_info(pk):
    d = _device(pk)
    return jsonify(at=d.last_info_at, at_local=settings.fmt_ts(d.last_info_at), info=d.last_info or {},
                   online=hub.is_online(d.id))


@bp.post("/<int:pk>/incidents/delete")
@admin_required
def incidents_delete(pk):
    device = _device(pk, allow_removed=True)
    body = request.get_json(silent=True) or {}
    ids = [int(i) for i in body.get("ids", []) if str(i).isdigit()]
    n = (Incident.query.filter(Incident.device_fk == device.id, Incident.id.in_(ids or [-1]))
         .delete(synchronize_session=False))
    db.session.commit()
    return jsonify(ok=True, deleted=n, message=f"Deleted {n} event(s)")


# ------------------------------------------------------------------ actions

def _delete_history(device):
    for model in (Measurement, Rollup, RollupDirty, Session, Incident, Command, Notification,
                  NotificationRule, MeterViewer):
        model.query.filter_by(device_fk=device.id).delete(synchronize_session=False)
    PairingCode.query.filter_by(device_fk=device.id).update({"device_fk": None})
    Event.query.filter_by(device_fk=device.id).delete(synchronize_session=False)


@bp.post("/<int:pk>/action")
@admin_required
def action(pk):
    device = _device(pk)
    body = request.get_json(silent=True) or request.form
    act = body.get("action")
    if act in ("relay_on", "relay_off"):
        cmd, err = commands.issue(device, "relay", {"on": act == "relay_on"}, current_user)
    elif act in ("renew", "reset_trip", "send_now"):
        params = {"by": current_user.username} if act == "reset_trip" else {}
        cmd, err = commands.issue(device, act, params, current_user)
    elif device.disabled_at and act not in ("send_now", "enable", "disable", "unsync", "remove", "get_info", "tare_now"):
        return jsonify(ok=False, error="disabled", message="This meter is disabled (read-only). Enable it first."), 409
    elif act == "set_default":
        device.cfg = devconfig.default_cfg(devconfig.channel_count(device))
        ensure_device_defaults(device, reset=True)
        device.cfg_ver_wanted += 1
        device.cfg_error = None
        ingest.server_event(device, "set_default", {"by": current_user.username})
        db.session.commit()
        _push_config(device)
        return jsonify(ok=True, message="Config reset to defaults" +
                       ("" if hub.is_online(device.id) else " (will apply when the device reconnects)"))
    elif act in ("disable", "enable", "unsync", "remove"):
        if act == "remove" and str(body.get("confirm", "")).strip().upper() != device.device_id:
            return jsonify(ok=False, error="confirm", message=f"Type the Device ID ({device.device_id}) to confirm."), 400
        if act == "enable" and not device.disabled_at:
            return jsonify(ok=False, error="not_disabled", message="This meter is not disabled."), 409
        getattr(lifecycle, act)(device, current_user)
        msgs = {"disable": "Meter disabled: session ended, relay off, history is read-only.",
                "enable": "Meter enabled: a new session starts.",
                "unsync": "Meter unsynced: it will factory reset. History is kept.",
                "remove": "Meter removed: it will factory reset and all its data was deleted."}
        flash(msgs[act], "success")
        dest = url_for("meters.info", pk=device.id) if act in ("disable", "enable") else url_for("main.dashboard")
        return jsonify(ok=True, redirect=dest)
    elif act == "tare_now":
        # Suggest offsets from the latest reading. Only meaningful with the relay OFF (then the
        # meter measures nothing but its own power supply). The form is filled, not saved.
        relays = (device.last_state or {}).get("relay") or []
        if relays and relays[0].get("on"):
            return jsonify(ok=False, error="relay_on",
                           message="Turn the relay OFF first: with the load off, the meter only measures itself."), 409
        reading = device.last_reading or {}
        ch = (reading.get("ch") or [{}])[0]
        if not reading.get("ts") or now() - reading["ts"] > 120 or ch.get("w") is None:
            return jsonify(ok=False, error="stale",
                           message="No reading from the last 2 minutes. Use Get Data Now on the Info page, then try again."), 409
        return jsonify(ok=True, a=round(ch.get("a") or 0, 3), w=round(ch.get("w") or 0, 1),
                       message=f"Filled from the reading at {settings.fmt_ts(reading['ts'])}. Press Save to keep it.")
    elif act == "get_info":
        cmd, err = commands.issue(device, "get_info", {}, current_user)
        if err or (cmd is not None and not cmd.sent_at):
            if cmd is not None:
                cmd.cancelled = True
                db.session.commit()
            return jsonify(ok=False, error="offline", message="The meter is offline."), 409
        return jsonify(ok=True, message="Asked the meter for its info")
    else:
        return jsonify(ok=False, error="unknown_action"), 400
    if err:
        messages = {
            "disabled": "This meter is disabled (read-only). Enable it first.",
            "quota_exceeded": "Relay ON blocked: kWh quota for this session is used up. Renew the session first.",
            "session_expired": "Relay ON blocked: session ended. Renew the session first.",
            "tripped": "Relay ON blocked: over-current trip. Reset the trip first.",
            "offline": "The device is offline.",
        }
        return jsonify(ok=False, error=err, message=messages.get(err, err)), 409
    sent = cmd.sent_at is not None
    return jsonify(ok=True, queued=not sent,
                   message="Sent to device" if sent else "Device offline: queued, will be sent on reconnect")


# ------------------------------------------------------------------ info / live / history

def _incident_filters():
    types = [t for t in request.args.getlist("type") if t in incidents.TYPES] or list(incidents.TYPES)
    start = settings.local_to_epoch(request.args.get("from", ""))
    end = settings.local_to_epoch(request.args.get("to", ""))
    return types, start, end


def _incident_query(device, types, start, end):
    q = Incident.query.filter(Incident.device_fk == device.id, Incident.type.in_(types))
    if start:
        q = q.filter((Incident.ended_at.is_(None)) | (Incident.ended_at >= start))
    if end:
        q = q.filter(Incident.started_at <= end)
    return q.order_by(Incident.started_at.desc())


def incident_detail_text(inc):
    d = inc.detail or {}
    parts = []
    if inc.type == "trip":
        if d.get("a") is not None:
            parts.append(f"{d.get('a')} A > {d.get('limit')} A" if d.get("limit") else f"{d.get('a')} A")
        if d.get("reset_by") or d.get("reset_src"):
            parts.append(f"reset by {d.get('reset_by') or '?'} ({d.get('reset_src') or '?'})")
    if inc.type == "offline":
        if d.get("cause") == "server_down":
            parts.append("server was down")
        elif d.get("cause") == "ping_timeout":
            parts.append("no reply (timeout)")
    if inc.type == "voltage":
        if d.get("v") is not None:
            parts.append(f"{d.get('v')} V ({'low' if d.get('dir') == 'low' else 'high'}), range {d.get('range', '?')}")
        if d.get("action"):
            parts.append({"end": "session ended", "hold": "relay off", "run": "power kept on"}.get(d["action"], d["action"]))
    if inc.type == "tamper" and d.get("action"):
        parts.append({"end": "session ended", "hold": "relay off", "run": "power kept on"}.get(d["action"], d["action"]))
    if d.get("opened_by") == "state_sync" or d.get("closed_by") == "state_sync":
        parts.append("time from state sync")
    return " · ".join(parts) or "–"


@bp.get("/<int:pk>")
@login_required
def info(pk):
    device = access.device_for_current_user(pk, allow_removed=True)
    types, start, end = _incident_filters()
    page = request.args.get("page", 1, type=int)
    pagination = _incident_query(device, types, start, end).paginate(page=page, per_page=25, error_out=False)
    sessions = (Session.query.filter_by(device_fk=device.id)
                .order_by(Session.started_at.desc()).limit(50).all())
    return render_template("meters/info.html", d=device, m=meter_summary(device),
                           pagination=pagination, incident_types=incidents.TYPES, sel_types=types,
                           f_from=request.args.get("from", ""), f_to=request.args.get("to", ""),
                           detail_text=incident_detail_text, sessions=sessions, now=now(),
                           cfg=device.cfg or {}, channels=device.channels,
                           price=settings.price() if access.can_price(current_user) else (None, None),
                           money=settings.money, can_csv=access.can_csv(current_user),
                           tare_kwh=lambda sess: (round(tare_svc.kwh(sess.kwh_used or 0, (sess.ended_at or now()) - sess.started_at,
                                                                     tare_svc.offsets(device)), 4)
                                                  if tare_svc.offsets(device) and sess.started_at else None))


_last_refresh = {}  # meter pk -> time of last "Get Data Now" (any user)


@bp.post("/<int:pk>/refresh")
@login_required
def refresh(pk):
    """Ask the meter for a fresh reading now ("Get Data Now"). Viewers may use it too."""
    device = access.device_for_current_user(pk)
    if not hub.is_online(device.id):
        return jsonify(ok=False, error="offline", message="The meter is offline."), 409
    if now() - _last_refresh.get(device.id, 0) < 5:
        return jsonify(ok=True, message="Already requested")
    _last_refresh[device.id] = now()
    cmd, err = commands.issue(device, "send_now", {}, current_user)
    if err:
        return jsonify(ok=False, error=err, message="The meter is offline."), 409
    return jsonify(ok=True, message="Requested")


@bp.get("/<int:pk>/live")
@login_required
def live(pk):
    return jsonify(meter_summary(access.device_for_current_user(pk, allow_removed=True)))


def _range_args(device):
    rng = request.args.get("range", "1d")
    # Exact epoch range (e.g. "Historical" on a session row) wins over the minute-precision inputs.
    start_ts, end_ts = request.args.get("start_ts", type=int), request.args.get("end_ts", type=int)
    if rng == "custom" and start_ts and end_ts and start_ts < end_ts:
        return start_ts, end_ts
    start = settings.local_to_epoch(request.args.get("from", ""))
    end = settings.local_to_epoch(request.args.get("to", ""))
    return history.resolve_range(device.id, rng, start, end)


@bp.get("/<int:pk>/history")
@login_required
def history_json(pk):
    device = access.device_for_current_user(pk, allow_removed=True)
    start, end = _range_args(device)
    ch = request.args.get("ch", 0, type=int)
    data = history.query(device.id, ch, start, end)
    if request.args.get("tared") == "1":
        data = history.apply_tare(data, tare_svc.offsets(device))
    data["start_local"] = settings.fmt_ts(start, False)
    data["end_local"] = settings.fmt_ts(end, False)
    data["tz_offset_s"] = _tz_offset_s()
    currency, price = settings.price()
    kwh = data["stats"].get("kwh")
    data["cost"] = (settings.money(kwh * price, currency)
                    if access.can_price(current_user) and price is not None and kwh is not None else None)
    return jsonify(data)


def _tz_offset_s():
    from datetime import datetime, timezone
    return int(datetime.now(timezone.utc).astimezone(settings.tz()).utcoffset().total_seconds())


def _utc(ts):
    from datetime import datetime, timezone
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _csv_tare(device, source, header, rows):
    """When a tare is set, add tared current/power columns next to the raw ones (raw data only)."""
    tare = tare_svc.offsets(device)
    if not tare or source != "raw":
        return header, rows
    ia, iw = header.index("A"), header.index("W")
    out = [(ts, list(vals) + [None if vals[ia] is None else round(tare_svc.amps(vals[ia], tare), 3),
                              None if vals[iw] is None else round(tare_svc.watts(vals[iw], tare), 1)])
           for ts, vals in rows]
    return header + [f"A_tared (-{tare[0]:g})", f"W_tared (-{tare[1]:g})"], out


def _csv_response(filename, header, rows):
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(header)
    w.writerows(rows)
    return Response(buf.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": f'attachment; filename="{filename}"'})


def _fname_ts(ts):
    return settings.fmt_ts(ts, False).replace(" ", "_").replace(":", "")


@bp.get("/<int:pk>/export.csv")
@login_required
def export_csv(pk):
    device = access.device_for_current_user(pk, allow_removed=True)
    if not access.can_csv(current_user):
        abort(403)
    ch = request.args.get("ch", 0, type=int)
    sess_id = request.args.get("session", type=int)
    if sess_id:  # one session: from its start to its end (or now, if still running)
        sess = db.session.get(Session, sess_id)
        if sess is None or sess.device_fk != device.id or not sess.started_at:
            abort(404)
        start, end = sess.started_at, sess.ended_at or now()
        source, header, rows = history.csv_rows(device.id, ch, start, end)
        header, rows = _csv_tare(device, source, header, rows)
        out = [[_utc(ts), settings.fmt_ts(ts), ch] + [("" if v is None else v) for v in vals] for ts, vals in rows]
        hdr = ["time_utc", f"time_local ({settings.get('timezone')})", "channel"] + header
        return _csv_response(f"{device.device_id}_session{sess.sid}_{_fname_ts(start)}.csv", hdr, out)
    start, end = _range_args(device)
    source, header, rows = history.csv_rows(device.id, ch, start, end)
    header, rows = _csv_tare(device, source, header, rows)
    out = [[_utc(ts), settings.fmt_ts(ts), ch] + [("" if v is None else v) for v in vals]
           for ts, vals in rows]
    hdr = ["time_utc", f"time_local ({settings.get('timezone')})", "channel"] + header
    return _csv_response(f"{device.device_id}_{_fname_ts(start)}_{_fname_ts(end)}.csv", hdr, out)


@bp.get("/<int:pk>/incidents.csv")
@login_required
def incidents_csv(pk):
    device = access.device_for_current_user(pk, allow_removed=True)
    if not access.can_csv(current_user):
        abort(403)
    types, start, end = _incident_filters()
    rows = []
    for inc in _incident_query(device, types, start, end).all():
        rows.append([incidents.TYPES.get(inc.type, inc.type),
                     _utc(inc.started_at), settings.fmt_ts(inc.started_at),
                     _utc(inc.ended_at) if inc.ended_at else "", settings.fmt_ts(inc.ended_at) if inc.ended_at else "ongoing",
                     inc.duration_s if inc.duration_s is not None else now() - inc.started_at,
                     int(inc.time_estimated), incident_detail_text(inc)])
    hdr = ["type", "started_utc", "started_local", "ended_utc", "ended_local", "duration_s",
           "time_estimated", "details"]
    return _csv_response(f"{device.device_id}_events.csv", hdr, rows)


# ------------------------------------------------------------------ bulk actions (dashboard)

def _bulk_devices(ids):
    devices = [d for d in (db.session.get(Device, i) for i in ids) if d is not None and not d.removed_at]
    return sorted(devices, key=lambda d: d.name.lower())


def _ids_arg(raw):
    return sorted({int(x) for x in str(raw or "").split(",") if x.strip().isdigit()})[:500]


@bp.post("/bulk/renew")
@admin_required
def bulk_renew():
    body = request.get_json(silent=True) or {}
    devices = _bulk_devices([int(i) for i in body.get("ids", []) if str(i).isdigit()])
    if not devices:
        return jsonify(ok=False, error="No meters selected"), 400
    sent = queued = 0
    for d in devices:
        cmd, err = commands.issue(d, "renew", {}, current_user)
        if cmd is not None and cmd.sent_at:
            sent += 1
        elif cmd is not None:
            queued += 1
    msg = f"Renew sent to {sent} meter(s)"
    if queued:
        msg += f"; {queued} offline meter(s) will renew when they reconnect"
    return jsonify(ok=True, message=msg, sent=sent, queued=queued)


# Settings that Bulk Configuration can change, each applied only when its "Change" switch is on.
BULK_GROUPS = {
    "report": "Send data every",
    "kwh": "kWh limit",
    "amp": "Max current",
    "session": "Session time limit & auto renew",
    "offline": "Relay off when offline",
    "voltage": "Operating Voltage Range",
    "tamper": "Tamper Protection",
    "viewers": "Linked users",
}


def _bulk_apply(cfg, new, groups):
    """Copy only the chosen groups of `new` into a meter's existing `cfg`."""
    out = dict(cfg)
    out["ch"] = [dict(c) for c in cfg.get("ch", [])]
    if "report" in groups:
        out["report_s"] = new["report_s"]
    if "session" in groups:
        for k in ("sess_mode", "dur_min", "daily_at", "auto"):
            out[k] = new[k]
    if "offline" in groups:
        out["offline_off"], out["offline_off_min"] = new["offline_off"], new["offline_off_min"]
    if "voltage" in groups:
        for k in ("volt_min", "volt_max", "volt_action"):
            out[k] = new[k]
    if "tamper" in groups:
        out["tamper_protect"], out["tamper_action"] = new["tamper_protect"], new["tamper_action"]
    for c in out["ch"]:  # the same limits for every channel of every selected meter
        if "kwh" in groups:
            c["kwh_max"], c["kwh_action"] = new["ch"][0]["kwh_max"], new["ch"][0]["kwh_action"]
            c.pop("kwh_end", None)
        if "amp" in groups:
            c["amp_max"] = new["ch"][0]["amp_max"]
    return out


def _differs(devices, fn):
    vals = {repr(fn(d.cfg or {})) for d in devices}
    return len(vals) > 1


@bp.route("/bulk/configure", methods=["GET", "POST"])
@admin_required
def bulk_configure():
    devices = _bulk_devices(_ids_arg(request.values.get("ids")))
    if not devices:
        flash("Select at least one meter on the dashboard first.", "warning")
        return redirect(url_for("main.dashboard"))
    first = devices[0]
    rating = min(devconfig.amp_rating(d) for d in devices)
    errors, results = [], None
    if request.method == "POST":
        form = request.form
        groups = {g for g in BULK_GROUPS if form.get(f"apply_{g}") == "on"}
        if not groups:
            errors.append("Switch on at least one section to change.")
        # Validate with a one-channel stand-in limited to the lowest current rating.
        probe = Device(device_id="BULK", name="bulk", caps={"channels": 1, "amp_rating": rating}, cfg={})
        new, cfg_errors = devconfig.parse_form(form, probe)
        errors += [e.replace("CH1 ", "") for e in cfg_errors]
        if not errors:
            viewer_mode = form.get("viewer_mode", "add")
            viewer_ids = {int(x) for x in form.getlist("viewers") if x.isdigit()}
            results = []
            for d in devices:
                cfg = _bulk_apply(d.cfg or devconfig.default_cfg(devconfig.channel_count(d)), new, groups)
                changed = cfg != d.cfg
                if changed:
                    d.cfg = cfg
                    d.cfg_ver_wanted += 1
                    d.cfg_error = None
                if "viewers" in groups:
                    current = {v.user_fk for v in MeterViewer.query.filter_by(device_fk=d.id)}
                    target = (current | viewer_ids if viewer_mode == "add" else
                              current - viewer_ids if viewer_mode == "remove" else viewer_ids)
                    _set_viewers(d, target)
                db.session.commit()
                if changed:
                    _push_config(d)
                results.append({"id": d.id, "name": d.name, "device_id": d.device_id, "changed": changed})
            group_names = ", ".join(sorted(BULK_GROUPS[g] for g in groups))
            log.info("Bulk configuration (%s) applied to %d meter(s) by %s", group_names, len(devices),
                     current_user.username)
    cfg = first.cfg or devconfig.default_cfg(devconfig.channel_count(first))
    dur = int(cfg.get("dur_min", 1440))
    ch0 = (cfg.get("ch") or [{}])[0]
    differs = {
        "report": _differs(devices, lambda c: c.get("report_s")),
        "kwh": _differs(devices, lambda c: [(x.get("kwh_max"), devconfig.kwh_action(x)) for x in c.get("ch", [])]),
        "amp": _differs(devices, lambda c: [x.get("amp_max") for x in c.get("ch", [])]),
        "session": _differs(devices, lambda c: (c.get("sess_mode"), c.get("dur_min"), c.get("daily_at"), c.get("auto"))),
        "offline": _differs(devices, lambda c: (c.get("offline_off"), c.get("offline_off_min"))),
        "voltage": _differs(devices, lambda c: (c.get("volt_min", 0), c.get("volt_max", 0), c.get("volt_action", "hold"))),
        "tamper": _differs(devices, lambda c: (bool(c.get("tamper_protect")), c.get("tamper_action", "hold"))),
    }
    return render_template("meters/bulk.html", devices=devices, ids=",".join(str(d.id) for d in devices),
                           cfg=cfg, ch0=ch0, dur_d=dur // 1440, dur_h=(dur % 1440) // 60, dur_m=dur % 60,
                           amp_rating=rating, kwh_actions=devconfig.KWH_ACTIONS, groups=BULK_GROUPS,
                           protect_actions=devconfig.PROTECT_ACTIONS, kwh_action_of=devconfig.kwh_action,
                           differs=differs, errors=errors, results=results, viewers=_viewers(),
                           form=request.form if errors else None)
