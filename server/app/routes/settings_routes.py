import secrets

from flask import Blueprint, abort, flash, jsonify, redirect, render_template, request, url_for
from flask_login import current_user, login_required, login_user

from ..extensions import db
from ..models import Device, MeterViewer, Notification, NotificationRule, PairingCode, User, now
from ..services import access, devconfig, hub, mqtt_bridge, notify, settings, validation
from .decorators import admin_required, staff_required

bp = Blueprint("settings", __name__, url_prefix="/settings")

TABS = ("account", "server", "users", "notifications", "mqtt")


@bp.get("/")
@login_required
def index():
    tab = request.args.get("tab", "account")
    allowed = {"account"} | ({"users"} if current_user.is_staff else set()) | (set(TABS) if current_user.is_admin else set())
    if tab not in allowed:
        tab = "account"
    host, port, tls, configured = settings.public_server(request)
    meters = access.managed_devices(current_user).all()
    names = {d.id: d.name for d in Device.query.filter(Device.removed_at.is_(None))}
    users = access.managed_users(current_user).order_by(User.role, User.username).all()
    user_meters, links = {}, {}
    for link in MeterViewer.query.all():
        if link.device_fk in names:
            label = names[link.device_fk]
            if link.channels:
                label += " (" + ", ".join(f"CH{int(c) + 1}" for c in link.channels) + ")"
            user_meters.setdefault(link.user_fk, []).append(label)
            links[(link.user_fk, link.device_fk)] = link
    owned = {}
    for d in Device.query.filter(Device.removed_at.is_(None), Device.owner_fk.isnot(None)):
        owned.setdefault(d.owner_fk, []).append(d.name)
    mgrs = access.managers() if current_user.is_admin else []
    return render_template(
        "settings/index.html", tab=tab, users=users, meters=meters, links=links, now=now(),
        user_meters={k: sorted(v) for k, v in user_meters.items()}, owned={k: sorted(v) for k, v in owned.items()},
        managers=mgrs, manager_names={m.id: m.username for m in mgrs},
        zones=settings.zone_names(), s=settings, detected=(host, port, tls), configured=configured,
        mqtt_status=mqtt_bridge.status, mqtt_meter=mqtt_bridge.meter_config())


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


@bp.post("/account/notifications")
@login_required
def account_notifications():
    days = (request.form.get("notif_autodel_days") or "30").strip()
    if not days.isdigit() or not 1 <= int(days) <= 3650:
        return _errors("account", ["Days must be 1–3650"])
    current_user.notif_autodel = request.form.get("notif_autodel") == "on"
    current_user.notif_autodel_days = int(days)
    db.session.commit()
    return _back("account", "Notification clean-up saved.")


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
    """Daily reset times (UTC minute-of-day) and AUX schedules (UTC offset) depend on the zone."""
    from ..services import auxcfg, scheduler
    settings.set_many({"tz_min_sent": str(auxcfg.tz_minutes())})
    scheduler.repush_local_time()


def _set_access(user, view_all, meter_ids):
    """Links of `user` to the meters the current account manages are replaced by `meter_ids`; links to
    other meters (made by the admin) stay. A kept link keeps its channel selection."""
    user.view_all = view_all
    scope = {d.id for d in access.managed_devices(current_user)}
    existing = {link.device_fk: link for link in MeterViewer.query.filter_by(user_fk=user.id)}
    for mid, link in existing.items():
        if mid in scope and mid not in meter_ids:
            db.session.delete(link)
    for mid in meter_ids:
        if mid in scope and mid not in existing:
            db.session.add(MeterViewer(device_fk=mid, user_fk=user.id))
    db.session.flush()
    _drop_unseen_rules(user)


def _drop_unseen_rules(user):
    """Notification options of meters the user can no longer see are dropped."""
    for r in NotificationRule.query.filter_by(user_fk=user.id).all():
        d = db.session.get(Device, r.device_fk)
        if d is not None and not access.can_view(user, d):
            db.session.delete(r)


def _ids(name):
    return {int(x) for x in request.form.getlist(name) if x.isdigit()}


def _role_label(u):
    return "Manager" if u.role == "manager" else "Viewer"


def _delete_user(user):
    if user.role == "manager":  # its meters go back to the admin, its viewers become the admin's
        Device.query.filter_by(owner_fk=user.id).update({"owner_fk": None})
        User.query.filter_by(manager_fk=user.id).update({"manager_fk": None})
        PairingCode.query.filter_by(created_by=user.id).update({"created_by": None})
    MeterViewer.query.filter_by(user_fk=user.id).delete()
    NotificationRule.query.filter_by(user_fk=user.id).delete()
    Notification.query.filter_by(user_fk=user.id).delete()
    db.session.delete(user)


@bp.post("/users")
@staff_required
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
        role = f.get("role") if current_user.is_admin and f.get("role") in ("viewer", "manager") else "viewer"
        manager_fk = current_user.id if current_user.is_manager else None
        if current_user.is_admin and role == "viewer" and f.get("manager", type=int):
            m = db.session.get(User, f.get("manager", type=int))
            if m is None or m.role != "manager":
                errs.append("Unknown manager")
            else:
                manager_fk = m.id
        if errs:
            return _errors("users", errs)
        u = User(username=username, role=role, email=email, phone=phone, manager_fk=manager_fk,
                 can_csv=f.get("can_csv") == "on", show_price=f.get("show_price") == "on")
        u.set_password(f["password"])
        db.session.add(u)
        db.session.flush()
        if role == "viewer":
            _set_access(u, f.get("view_all") == "on", _ids("meters"))
        db.session.commit()
        return _back("users", f"{_role_label(u)} '{username}' created.")
    if what == "bulk":
        action = f.get("action")
        q = access.managed_users(current_user)
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
        return _back("users", f"{action.capitalize()}d {n} account(s)." if n else "No accounts selected.",
                     "success" if n else "warning")
    if what == "login_policy":
        if not current_user.is_admin:
            abort(403)
        attempts, minutes = (f.get("login_max_attempts") or "").strip(), (f.get("login_lock_minutes") or "").strip()
        if not attempts.isdigit() or int(attempts) > 100 or not minutes.isdigit() or int(minutes) > 10080:
            return _errors("users", ["Attempts must be 0–100 and minutes 0–10080"])
        settings.set_many({"login_max_attempts": attempts, "login_lock_minutes": minutes})
        return _back("users", "Login protection saved.")
    user = db.session.get(User, f.get("user_id", type=int))
    if not access.can_manage_user(current_user, user):
        abort(404)
    label = f"{_role_label(user)} '{user.username}'"
    if what == "toggle":
        user.is_enabled = not user.is_enabled
        if not user.is_enabled:
            user.session_token = secrets.token_hex(16)
        msg = f"{label} {'enabled' if user.is_enabled else 'disabled'}."
    elif what == "unlock":
        user.failed_logins, user.locked_until = 0, None
        msg = f"{label} unlocked."
    elif what == "access":
        if user.role == "viewer":
            if current_user.is_admin and "manager" in f:  # move the viewer to another manager / the admin
                mid = f.get("manager", type=int)
                m = db.session.get(User, mid) if mid else None
                if mid and (m is None or m.role != "manager"):
                    abort(400)
                user.manager_fk = m.id if m else None
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
        _delete_user(user)
        msg = f"{label} deleted."
    else:
        abort(400)
    db.session.commit()
    return _back("users", msg)


@bp.post("/notifications")
@admin_required
def notifications():
    f = request.form
    values = {"telegram_enabled": "1" if f.get("telegram_enabled") == "on" else "0"}
    # The token is treated like a password: never sent back to the browser. Empty = keep the saved one.
    token = (f.get("telegram_bot_token") or "").strip()
    if f.get("telegram_clear") == "on":
        values["telegram_bot_token"] = ""
    elif token:
        values["telegram_bot_token"] = token
    settings.set_many(values)
    return _back("notifications", "Notification settings saved.")


def _mqtt_form(f):
    """Broker settings from the form -> (values to store, errors). An empty password keeps the saved one."""
    errs = []

    def host(key, label):
        v = (f.get(key) or "").strip()
        for pre in ("wss://", "ws://", "https://", "http://", "mqtts://", "mqtt://"):
            v = v.removeprefix(pre)
        v = v.rstrip("/")
        if len(v) > 100 or any(c in v for c in " /?#@"):
            errs.append(f"{label}: a host name or IP only (no ws://, no path)")
        return v

    def port(key, label):
        v = (f.get(key) or "").strip()
        if v and (not v.isdigit() or not 1 <= int(v) <= 65535):
            errs.append(f"{label} must be 1–65535 or empty")
        return v

    def path(key):
        v = (f.get(key) or "/").strip() or "/"
        return v if v.startswith("/") else "/" + v

    prefix = (f.get("mqtt_prefix") or "powermanager").strip().strip("/")
    if not prefix or len(prefix) > 48 or any(c in prefix for c in "#+ "):
        errs.append("Topic prefix: 1–48 characters, no spaces, # or +")
    user = (f.get("mqtt_user") or "").strip()
    if len(user) > 64:
        errs.append("User name: up to 64 characters")
    values = {
        "mqtt_enabled": "1" if f.get("mqtt_enabled") == "on" else "0",
        "mqtt_host": host("mqtt_host", "Broker address"), "mqtt_port": port("mqtt_port", "Port"),
        "mqtt_tls": "1" if f.get("mqtt_tls") == "on" else "0",
        "mqtt_transport": "tcp" if f.get("mqtt_transport") == "tcp" else "websockets",
        "mqtt_path": path("mqtt_path"), "mqtt_user": user, "mqtt_prefix": prefix,
        "mqtt_pub_host": host("mqtt_pub_host", "Address for the meters"),
        "mqtt_pub_port": port("mqtt_pub_port", "Port for the meters"),
        "mqtt_pub_tls": "1" if f.get("mqtt_pub_tls") == "on" else "0", "mqtt_pub_path": path("mqtt_pub_path"),
    }
    pw = f.get("mqtt_pass") or ""
    if len(pw) > 128:
        errs.append("Password: up to 128 characters")
    if f.get("mqtt_pass_clear") == "on":
        values["mqtt_pass"] = ""
    elif pw:
        values["mqtt_pass"] = pw
    if values["mqtt_enabled"] == "1" and not values["mqtt_host"]:
        errs.append("Enter the broker address (or switch MQTT off)")
    return values, errs


@bp.post("/mqtt")
@admin_required
def mqtt():
    values, errs = _mqtt_form(request.form)
    if errs:
        return _errors("mqtt", errs)
    settings.set_many(values)
    n = mqtt_bridge.push_settings_to_meters()  # meters that follow the server's broker get the new settings
    msg = "MQTT settings saved."
    if n:
        msg += f" Sent to {n} meter(s) (meters that are offline get them when they reconnect)."
    if values["mqtt_enabled"] == "1" and mqtt_bridge.meter_config() is None:
        flash("The server uses plain TCP and no WebSocket address for the meters is set: meters keep using the "
              "server's WebSocket.", "warning")
    return _back("mqtt", msg)


@bp.post("/mqtt/test")
@admin_required
def mqtt_test():
    """Test connection with the values in the form (not saved); an empty password = the saved one."""
    f = request.get_json(silent=True) or {}
    values, errs = _mqtt_form({k: ("on" if v is True else ("" if v in (False, None) else str(v))) for k, v in f.items()})
    if errs:
        return jsonify(ok=False, message=errs[0])
    tls = values["mqtt_tls"] == "1"
    transport = values["mqtt_transport"]
    port = int(values["mqtt_port"]) if values["mqtt_port"] else mqtt_bridge.default_port(transport, tls)
    cfg = {"host": values["mqtt_host"], "port": port, "tls": tls, "transport": transport, "path": values["mqtt_path"],
           "user": values["mqtt_user"], "pass": values.get("mqtt_pass", settings.get("mqtt_pass"))}
    ok, msg = mqtt_bridge.test_connection(cfg)
    return jsonify(ok=ok, message=f"{msg} ({'wss' if tls and transport == 'websockets' else 'ws' if transport == 'websockets' else 'mqtts' if tls else 'mqtt'}://{cfg['host']}:{port})")


@bp.post("/notifications/test")
@login_required
def notifications_test():
    """Send a test message to the current user's own Telegram chat."""
    if settings.get("telegram_enabled") != "1":
        return jsonify(ok=False, error="Telegram is not enabled by the admin")
    ok, err = notify.send_telegram(settings.get("telegram_bot_token"), current_user.telegram_chat_id,
                                   "PowerManager test message ✅")
    return jsonify(ok=ok, error=err)
