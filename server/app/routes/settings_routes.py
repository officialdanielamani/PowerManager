from flask import Blueprint, abort, flash, jsonify, redirect, render_template, request, url_for
from flask_login import current_user, login_required, login_user

from ..extensions import db
from ..models import Device, User
from ..services import devconfig, hub, notify, settings, validation
from .decorators import admin_required

bp = Blueprint("settings", __name__, url_prefix="/settings")

TABS = ("account", "server", "users", "notifications")


@bp.get("/")
@login_required
def index():
    tab = request.args.get("tab", "account")
    if tab not in TABS or (tab != "account" and not current_user.is_admin):
        tab = "account"
    host, port, tls, configured = settings.public_server(request)
    return render_template(
        "settings/index.html", tab=tab, users=User.query.order_by(User.role, User.username).all(),
        zones=settings.zone_names(), s=settings, detected=(host, port, tls), configured=configured)


def _back(tab, msg=None, cat="success"):
    if msg:
        flash(msg, cat)
    return redirect(url_for("settings.index", tab=tab))


def _errors(tab, errs):
    for e in errs:
        flash(e, "danger")
    return redirect(url_for("settings.index", tab=tab))


@bp.post("/account")
@login_required
def account():
    f = request.form
    if not current_user.check_password(f.get("current_password", "")):
        return _back("account", "Current password is wrong", "danger")
    what = f.get("what")
    if what == "username":
        username, err = validation.username(f.get("username"))
        if err:
            return _errors("account", [err])
        other = User.query.filter_by(username=username).first()
        if other is not None and other.id != current_user.id:
            return _errors("account", ["That username is already taken"])
        current_user.username = username
    elif what == "password":
        err = validation.password(f.get("password"), f.get("confirm"))
        if err:
            return _errors("account", [err])
        current_user.set_password(f["password"])  # rotates session token -> other sessions end
        db.session.commit()
        login_user(current_user)
        return _back("account", "Password changed. Other sessions were logged out.")
    elif what == "phone":
        phone, err = validation.phone(f.get("phone"), required=current_user.is_admin)
        if err:
            return _errors("account", [err])
        current_user.phone = phone
    elif what == "email":
        email, err = validation.email(f.get("email"), required=current_user.is_admin)
        if err:
            return _errors("account", [err])
        current_user.email = email
    else:
        abort(400)
    db.session.commit()
    return _back("account", "Saved.")


@bp.post("/server")
@admin_required
def server():
    f = request.form
    errs = []
    address = (f.get("server_address") or "").strip()
    address = address.removeprefix("https://").removeprefix("http://").rstrip("/")
    if len(address) > 253 or any(c in address for c in " /?#@"):
        errs.append("Server address must be a host name or IP (no http://, no path)")
    port = (f.get("server_port") or "").strip()
    if port and (not port.isdigit() or not 1 <= int(port) <= 65535):
        errs.append("Port must be 1–65535 or empty")
    zone = f.get("timezone", "UTC")
    if zone not in settings.zone_names():
        errs.append("Unknown time zone")
    retention = (f.get("raw_retention_days") or "90").strip()
    if not retention.isdigit() or not 7 <= int(retention) <= 3650:
        errs.append("Raw data retention must be 7–3650 days")
    if errs:
        return _errors("server", errs)
    old_zone = settings.get("timezone")
    settings.set_many({"server_address": address, "server_port": port,
                       "server_tls": "1" if f.get("server_tls") == "on" else "0",
                       "timezone": zone, "raw_retention_days": retention})
    if zone != old_zone:
        _repush_daily_configs()
    return _back("server", "Server settings saved.")


def _repush_daily_configs():
    """The daily reset time is sent as UTC minute-of-day, so a zone change needs a new config."""
    for d in Device.query.filter(Device.removed_at.is_(None)).all():
        if (d.cfg or {}).get("sess_mode") == "daily":
            d.cfg_ver_wanted += 1
            db.session.commit()
            hub.send(d.id, {"t": "cfg", **devconfig.to_device(d.cfg, d.cfg_ver_wanted)})


@bp.post("/users")
@admin_required
def users():
    f = request.form
    what = f.get("what")
    if what == "add":
        errs = []
        username, err = validation.username(f.get("username"))
        errs += [err] if err else []
        if not err and User.query.filter_by(username=username).first():
            errs.append("That username is already taken")
        err = validation.password(f.get("password"), f.get("confirm"))
        errs += [err] if err else []
        email, err = validation.email(f.get("email"), required=False)
        errs += [err] if err else []
        phone, err = validation.phone(f.get("phone"), required=False)
        errs += [err] if err else []
        if errs:
            return _errors("users", errs)
        u = User(username=username, role="viewer", email=email, phone=phone)
        u.set_password(f["password"])
        db.session.add(u)
        db.session.commit()
        return _back("users", f"Viewer '{username}' created.")
    user = db.session.get(User, f.get("user_id", type=int))
    if user is None or user.role != "viewer":
        abort(404)
    if what == "toggle":
        user.is_enabled = not user.is_enabled
        msg = f"Viewer '{user.username}' {'enabled' if user.is_enabled else 'disabled'}."
    elif what == "reset_password":
        err = validation.password(f.get("password"), f.get("confirm"))
        if err:
            return _errors("users", [err])
        user.set_password(f["password"])
        msg = f"Password for '{user.username}' changed."
    elif what == "delete":
        db.session.delete(user)
        msg = f"Viewer '{user.username}' deleted."
    else:
        abort(400)
    db.session.commit()
    return _back("users", msg)


@bp.post("/notifications")
@admin_required
def notifications():
    f = request.form
    settings.set_many({
        "telegram_enabled": "1" if f.get("telegram_enabled") == "on" else "0",
        "telegram_bot_token": (f.get("telegram_bot_token") or "").strip(),
        "telegram_chat_id": (f.get("telegram_chat_id") or "").strip(),
    })
    return _back("notifications", "Notification settings saved.")


@bp.post("/notifications/test")
@admin_required
def notifications_test():
    ok, err = notify.send_telegram(settings.get("telegram_bot_token"), settings.get("telegram_chat_id"),
                                   "PowerManager test message ✅")
    return jsonify(ok=ok, error=err)
