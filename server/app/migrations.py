"""Small in-place schema upgrades for SQLite (the app uses create_all, which never alters tables).

Each step checks the live schema, so running it again is harmless.
"""
import logging

from sqlalchemy import inspect, text

from .extensions import db

log = logging.getLogger(__name__)


def _columns(table):
    insp = inspect(db.engine)
    if table not in insp.get_table_names():
        return None
    return {c["name"] for c in insp.get_columns(table)}


def _add_column(table, ddl):
    name = ddl.split()[0]
    cols = _columns(table)
    if cols is not None and name not in cols:
        db.session.execute(text(f"ALTER TABLE {table} ADD COLUMN {ddl}"))
        log.info("Migration: added %s.%s", table, name)


def run():
    # v0.1 -> v0.2: viewer links, per-user notifications, login lockout
    _add_column("user", "view_all BOOLEAN NOT NULL DEFAULT 0")
    _add_column("user", "failed_logins INTEGER NOT NULL DEFAULT 0")
    _add_column("user", "locked_until INTEGER")
    _add_column("user", "telegram_chat_id VARCHAR(32) NOT NULL DEFAULT ''")
    _add_column("notification", "user_fk INTEGER REFERENCES user(id)")

    # v0.4 -> v0.5: removal options, retention, meter info
    _add_column("user", "notif_autodel BOOLEAN NOT NULL DEFAULT 0")
    _add_column("user", "notif_autodel_days INTEGER NOT NULL DEFAULT 30")
    for ddl in ("purged BOOLEAN NOT NULL DEFAULT 0", "disabled_at INTEGER",
                "data_autodel BOOLEAN NOT NULL DEFAULT 0", "data_autodel_days INTEGER NOT NULL DEFAULT 120",
                "events_autodel BOOLEAN NOT NULL DEFAULT 0", "events_autodel_days INTEGER NOT NULL DEFAULT 120",
                "last_info JSON NOT NULL DEFAULT '{}'", "last_info_at INTEGER",
                "resync_pending BOOLEAN NOT NULL DEFAULT 0",
                "tare_a FLOAT NOT NULL DEFAULT 0", "tare_w FLOAT NOT NULL DEFAULT 0",
                "link_key VARCHAR(64)", "link_up INTEGER NOT NULL DEFAULT 0", "via_fk INTEGER"):
        _add_column("device", ddl)

    # v0.3 -> v0.4: per-viewer CSV / price permissions
    _add_column("user", "can_csv BOOLEAN NOT NULL DEFAULT 0")
    _add_column("user", "show_price BOOLEAN NOT NULL DEFAULT 0")

    # v0.2 -> v0.3: price per session
    _add_column("meter_session", "price_per_kwh FLOAT")
    _add_column("meter_session", "currency VARCHAR(8)")
    # v0.7: multi-channel meters (independent mode: one session per channel)
    _add_column("meter_session", "ch INTEGER")
    # v0.7.1: hardware change detection
    _add_column("device", "hw_sig VARCHAR(40)")
    # v0.7.2: sub-second readings
    _add_column("measurement", "ms INTEGER")

    cols = _columns("notification_rule")
    if cols is not None and "user_fk" not in cols:
        # Old rules were per meter; give them to the admin, then rebuild with the new key.
        db.session.execute(text("ALTER TABLE notification_rule RENAME TO notification_rule_v1"))
        db.session.commit()
        db.metadata.tables["notification_rule"].create(db.engine)
        db.session.execute(text(
            "INSERT INTO notification_rule (device_fk, user_fk, type, enabled, param) "
            "SELECT r.device_fk, u.id, r.type, r.enabled, r.param FROM notification_rule_v1 r "
            "JOIN user u ON u.role = 'admin'"))
        db.session.execute(text("UPDATE notification SET user_fk = (SELECT id FROM user WHERE role='admin') "
                                "WHERE user_fk IS NULL"))
        db.session.execute(text("DROP TABLE notification_rule_v1"))
        db.session.execute(text(
            "UPDATE user SET telegram_chat_id = COALESCE((SELECT value FROM setting "
            "WHERE key='telegram_chat_id'), '') WHERE role='admin' AND telegram_chat_id = ''"))
        log.info("Migration: notification rules are now per user (existing ones kept for the admin)")
    db.session.commit()
