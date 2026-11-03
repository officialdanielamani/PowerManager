import hmac

from flask import Blueprint, abort, current_app, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required, login_user, logout_user

from ..extensions import db, limiter
from ..models import User, now
from ..services import settings, validation

bp = Blueprint("auth", __name__)


@bp.route("/setup", methods=["GET", "POST"])
def setup():
    if User.query.filter_by(role="admin").first() is not None:
        abort(404)
    need_token = bool(current_app.config.get("SETUP_TOKEN"))
    errors, form = [], request.form
    if request.method == "POST":
        if need_token and not hmac.compare_digest(form.get("setup_token", "").strip(),
                                                  current_app.config["SETUP_TOKEN"]):
            errors.append("Setup token is wrong (see the server log)")
        username, err = validation.username(form.get("username"))
        errors += [err] if err else []
        err = validation.password(form.get("password"), form.get("confirm"))
        errors += [err] if err else []
        phone, err = validation.phone(form.get("phone"))
        errors += [err] if err else []
        email, err = validation.email(form.get("email"))
        errors += [err] if err else []
        if not errors:
            user = User(username=username, role="admin", phone=phone, email=email)
            user.set_password(form["password"])
            db.session.add(user)
            db.session.commit()
            current_app.config["_ADMIN_EXISTS"] = True
            login_user(user)
            flash("Admin account created. Welcome to PowerManager!", "success")
            return redirect(url_for("main.dashboard"))
    return render_template("setup.html", errors=errors, form=form, need_token=need_token)


def is_locked(user):
    """Viewer accounts lock after too many failed logins (Settings → Users → Login protection).
    The admin is never locked (an attacker could otherwise lock you out); the per-IP rate
    limit still applies to everyone."""
    if user.role != "viewer" or not user.locked_until:
        return False
    if user.locked_until == -1:
        return True
    if user.locked_until > now():
        return True
    user.locked_until = None
    user.failed_logins = 0
    return False


def register_failure(user):
    if user.role != "viewer":
        return
    limit = settings.get_int("login_max_attempts", 5)
    user.failed_logins = (user.failed_logins or 0) + 1
    if limit and user.failed_logins >= limit:
        minutes = settings.get_int("login_lock_minutes", 15)
        user.locked_until = now() + minutes * 60 if minutes else -1
    db.session.commit()


@bp.route("/login", methods=["GET", "POST"])
@limiter.limit("5 per minute", methods=["POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("main.dashboard"))
    error = None
    if request.method == "POST":
        username = (request.form.get("username") or "").strip().lower()
        user = User.query.filter_by(username=username).first()
        if user is not None and is_locked(user):
            error = "This account is locked after too many failed logins. Ask the admin to unlock it."
        elif user is None or not user.check_password(request.form.get("password", "")):
            error = "Wrong username or password"
            if user is not None:
                register_failure(user)
        elif not user.is_enabled:
            error = "This account is disabled"
        else:
            user.last_login_at = now()
            user.failed_logins = 0
            user.locked_until = None
            db.session.commit()
            login_user(user, remember=request.form.get("remember") == "on")
            nxt = request.args.get("next", "")
            if not nxt.startswith("/") or nxt.startswith("//"):
                nxt = url_for("main.dashboard")
            return redirect(nxt)
    return render_template("login.html", error=error)


@bp.post("/logout")
@login_required
def logout():
    logout_user()
    return redirect(url_for("auth.login"))
