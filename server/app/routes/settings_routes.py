import secrets

from flask import Blueprint, abort, flash, jsonify, redirect, render_template, request, url_for
from flask_login import current_user, login_required, login_user

from ..extensions import db
from ..models import Device, MeterViewer, NotificationRule, User, now
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
    names = {d.id: d.name for d in Device.query.filter(Device.removed_at.is_(None))}
    user_meters = {}
    for link in MeterViewer.query.all():
        if link.device_fk in names:
            user_meters.setdefault(link.user_fk, []).append(names[link.device_fk])
    return render_template(
        "settings/index.html", tab=tab, users=User.query.order_by(User.role, User.username).all(),
        meters=Device.query.filter(Device.removed_at.is_(None)).order_by(Device.name).all(),
        links={(m.user_fk, m.device_fk) for m in MeterViewer.query.all()}, now=now(),
        user_meters={k: sorted(v) for k, v in user_meters.items()},
        zones=settings.zone_names(), s=settings, detected=(host, port, tls), configured=configured)


def _back(tab, msg=None, cat="success"):
    if msg:
        flash(msg, cat)
    return redirect(url_for("settings.index", tab=tab))


def _errors(tab, errs):
    for e in errs:
        flash(e, "danger")
    return redirect(url_for("settings.index", tab=tab))


@bp.post("/account/telegram")
@login_required
def account_telegram():
    chat = (request.form.get("telegram_chat_id") or "").strip()
    if chat and not (chat.lstrip("-").isdigit() and len(chat) <= 20):
        return _errors("account", ["Telegram chat ID is a number, e.g. 123456789"])
    current_user.telegram_chat_id = chat
    db.session.commit()
    return _back("account", "Telegram chat ID saved.")


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
    currency = (f.get("currency") or "").strip().upper()
    if len(currency) > 8 or any(c in currency for c in "<>\"'&"):
        errs.append("Currency: up to 8 characters, e.g. MYR")
    price = (f.get("price_per_kwh") or "").strip()
    if price:
        try:
            pv = float(price)
            if not 0 <= pv <= 1000:
                raise ValueError
            price = f"{pv:.4f}".rstrip("0").rstrip(".")  # 0.2100 -> 0.21
        except ValueError:
            errs.append("Price per kWh must be a number between 0 and 1000, e.g. 0.21")
    if errs:
        return _errors("server", errs)
    old_zone = settings.get("timezone")
    settings.set_many({"server_address": address, "server_port": port,
                       "server_tls": "1" if f.get("server_tls") == "on" else "0",
                       "timezone": zone, "raw_retention_days": retention,
                       "currency": currency, "price_per_kwh": price})
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


def _set_access(user, view_all, meter_ids):
    user.view_all = view_all
    MeterViewer.query.filter_by(user_fk=user.id).delete()
    valid = {d.id for d in Device.query.all()}
    for mid in meter_ids:
        if mid in valid:
            db.session.add(MeterViewer(device_fk=mid, user_fk=user.id))
    if not view_all:  # drop notification options for meters the user can no longer see
        (NotificationRule.query.filter(NotificationRule.user_fk == user.id,
                                       NotificationRule.device_fk.notin_(meter_ids or [-1]))
         .delete(synchronize_session=False))


def _ids(name):
    return {int(x) for x in request.form.getlist(name) if x.isdigit()}


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
        u = User(username=username, role="viewer", email=email, phone=phone,
                 can_csv=f.get("can_csv") == "on", show_price=f.get("show_price") == "on")
        u.set_password(f["password"])
        db.session.add(u)
        db.session.flush()
        _set_access(u, f.get("view_all") == "on", _ids("meters"))
        db.session.commit()
        return _back("users", f"Viewer '{username}' created.")
    if what == "bulk":
        action = f.get("action")
        q = User.query.filter_by(role="viewer")
        if f.get("scope") != "all":
            q = q.filter(User.id.in_(_ids("user_ids") or {-1}))
        n = 0
        for u in q.all():
            if action == "disable":
                u.is_enabled = False
                u.session_token = secrets.token_hex(16)  # log them out now
            elif action == "enable":
                u.is_enabled = True
            elif action == "unlock":
                u.failed_logins, u.locked_until = 0, None
            else:
                abort(400)
            n += 1
        db.session.commit()
        return _back("users", f"{action.capitalize()}d {n} viewer account(s)." if n else "No accounts selected.",
                     "success" if n else "warning")
    if what == "login_policy":
        attempts, minutes = (f.get("login_max_attempts") or "").strip(), (f.get("login_lock_minutes") or "").strip()
        if not attempts.isdigit() or int(attempts) > 100 or not minutes.isdigit() or int(minutes) > 10080:
            return _errors("users", ["Attempts must be 0–100 and minutes 0–10080"])
        settings.set_many({"login_max_attempts": attempts, "login_lock_minutes": minutes})
        return _back("users", "Login protection saved.")
    user = db.session.get(User, f.get("user_id", type=int))
    if user is None or user.role != "viewer":
        abort(404)
    if what == "toggle":
        user.is_enabled = not user.is_enabled
        if not user.is_enabled:
            user.session_token = secrets.token_hex(16)
        msg = f"Viewer '{user.username}' {'enabled' if user.is_enabled else 'disabled'}."
    elif what == "unlock":
        user.failed_logins, user.locked_until = 0, None
        msg = f"Viewer '{user.username}' unlocked."
    elif what == "access":
        _set_access(user, f.get("view_all") == "on", _ids("meters"))
        user.can_csv = f.get("can_csv") == "on"
        user.show_price = f.get("show_price") == "on"
        msg = f"Access for '{user.username}' saved."
    elif what == "reset_password":
        err = validation.password(f.get("password"), f.get("confirm"))
        if err:
            return _errors("users", [err])
        user.set_password(f["password"])
        user.failed_logins, user.locked_until = 0, None
        msg = f"Password for '{user.username}' changed (account unlocked, logged out everywhere)."
    elif what == "delete":
        MeterViewer.query.filter_by(user_fk=user.id).delete()
        NotificationRule.query.filter_by(user_fk=user.id).delete()
        from ..models import Notification
        Notification.query.filter_by(user_fk=user.id).delete()
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
    })
    return _back("notifications", "Notification settings saved.")


@bp.post("/notifications/test")
@login_required
def notifications_test():
    """Send a test message to the current user's own Telegram chat."""
    if settings.get("telegram_enabled") != "1":
        return jsonify(ok=False, error="Telegram is not enabled by the admin")
    ok, err = notify.send_telegram(settings.get("telegram_bot_token"), current_user.telegram_chat_id,
                                   "PowerManager test message ✅")
    return jsonify(ok=ok, error=err)
