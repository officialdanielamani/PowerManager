"""PowerManager server."""
import logging
import secrets

from flask import Flask, jsonify, redirect, request, url_for
from flask_login import current_user
from sqlalchemy import event
from werkzeug.middleware.proxy_fix import ProxyFix

from .config import Config
from .extensions import csrf, db, limiter, login_manager, sock

__version__ = "0.3.0"
log = logging.getLogger("powermanager")


def _sqlite_pragmas(dbapi_conn, _record):
    cur = dbapi_conn.cursor()
    cur.execute("PRAGMA journal_mode=WAL")
    cur.execute("PRAGMA synchronous=NORMAL")
    cur.execute("PRAGMA busy_timeout=15000")
    cur.execute("PRAGMA foreign_keys=ON")
    cur.close()


def create_app(overrides=None):
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("apscheduler").setLevel(logging.WARNING)
    app = Flask(__name__)
    app.config.from_object(Config())
    if overrides:
        app.config.update(overrides)
    # Behind Cloudflare Tunnel / reverse proxy: trust X-Forwarded-Proto/Host/For (one hop).
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_port=1)

    db.init_app(app)
    csrf.init_app(app)
    limiter.init_app(app)
    sock.init_app(app)
    login_manager.init_app(app)
    login_manager.login_view = "auth.login"
    login_manager.login_message_category = "warning"

    from . import models
    from .routes import auth, device_api, main, meters, settings_routes

    app.register_blueprint(auth.bp)
    app.register_blueprint(main.bp)
    app.register_blueprint(meters.bp)
    app.register_blueprint(settings_routes.bp)
    app.register_blueprint(device_api.bp)
    csrf.exempt(device_api.bp)

    @login_manager.user_loader
    def load_user(token):
        try:
            uid, sess = token.split(":", 1)
            user = db.session.get(models.User, int(uid))
        except (ValueError, AttributeError):
            return None
        if user is None or user.session_token != sess or not user.is_enabled:
            return None
        return user

    with app.app_context():
        event.listen(db.engine, "connect", _sqlite_pragmas)
        from . import migrations
        migrations.run()  # upgrade tables created by an older version first
        db.create_all()
        _setup_token(app)
        if not app.config["TESTING"]:
            from .services import ingest
            ingest.mark_all_offline_on_startup()

    @app.before_request
    def require_setup():
        if request.endpoint in ("static", "auth.setup", "main.healthz", "device_api.pair", None):
            return None
        if request.path.startswith("/ws/"):
            return None
        if not app.config.get("_ADMIN_EXISTS"):
            if models.User.query.filter_by(role="admin").first() is None:
                return redirect(url_for("auth.setup"))
            app.config["_ADMIN_EXISTS"] = True
        return None

    @app.context_processor
    def inject():
        from .models import Notification
        unread = 0
        if current_user.is_authenticated:
            unread = Notification.query.filter_by(user_fk=current_user.id, read=False).count()
        return {"app_version": __version__, "unread_notifications": unread}

    from .services import incidents, settings
    app.jinja_env.filters["dt"] = settings.fmt_ts
    app.jinja_env.filters["duration"] = incidents.fmt_duration
    app.jinja_env.globals["tz_label"] = lambda: f"{settings.get('timezone')} ({settings.utc_offset_str()})"

    @app.errorhandler(404)
    def not_found(_e):
        if request.path.startswith("/api/"):
            return jsonify(err="not_found"), 404
        from flask import render_template
        return render_template("errors/404.html"), 404

    @app.errorhandler(429)
    def rate_limited(_e):
        if request.path.startswith("/api/"):
            return jsonify(err="rate_limited"), 429
        from flask import render_template
        return render_template("errors/429.html"), 429

    if app.config.get("START_SCHEDULER") and not app.config["TESTING"]:
        from .services import hub, scheduler, telegram_bot
        scheduler.start(app)
        telegram_bot.start(app)  # interactive bot (idles until Telegram is enabled with a token)
        hub.install_signal_handler()

    return app


def _setup_token(app):
    """Protect first-run setup on an exposed server (docs/architecture.md §3)."""
    from .models import User
    if User.query.filter_by(role="admin").first() is not None:
        app.config["_ADMIN_EXISTS"] = True
        return
    token = app.config.get("SETUP_TOKEN", "")
    if token.lower() == "off":
        app.config["SETUP_TOKEN"] = ""
        return
    if not token:
        token = secrets.token_urlsafe(9)
        app.config["SETUP_TOKEN"] = token
    log.warning("=" * 60)
    log.warning("  First-run setup token: %s", token)
    log.warning("  Open the web UI and enter this token to create the admin.")
    log.warning("=" * 60)
