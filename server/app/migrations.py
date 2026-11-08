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
    # v0.7.3: data clean-up split into sessions / events / historical data. "data" used to delete the
    # finished sessions too: keep doing that for meters that had it on.
    cols = _columns("device")
    if cols is not None and "sessions_autodel" not in cols:
        _add_column("device", "sessions_autodel BOOLEAN NOT NULL DEFAULT 0")
        _add_column("device", "sessions_autodel_days INTEGER NOT NULL DEFAULT 120")
        db.session.execute(text("UPDATE device SET sessions_autodel = data_autodel, sessions_autodel_days = data_autodel_days"))

    # v0.8: manager accounts, meter owner, per-meter price, positive power only, per-channel viewers
    _add_column("user", "manager_fk INTEGER REFERENCES user(id)")
    _add_column("meter_viewer", "channels JSON")
    cols = _columns("device")
    if cols is not None and "price_per_kwh" not in cols:
        _add_column("device", "owner_fk INTEGER REFERENCES user(id)")
        _add_column("device", "currency VARCHAR(8) NOT NULL DEFAULT ''")
        _add_column("device", "price_per_kwh FLOAT DEFAULT 0.25")
        _add_column("device", "positive_only BOOLEAN NOT NULL DEFAULT 1")
        # The price used to be one setting per meter type: give each meter the rate of its type.
        old = {r[0]: r[1] for r in db.session.execute(text(
            "SELECT key, value FROM setting WHERE key IN ('currency','price_per_kwh','dc_currency','dc_price_per_kwh')"))}
        for pre, cond in (("", "COALESCE(json_extract(caps, '$.kind'), 'ac') != 'dc'"),
                          ("dc_", "json_extract(caps, '$.kind') = 'dc'")):
            try:
                price = float(old.get(pre + "price_per_kwh") or "")
            except ValueError:
                price = 0.25
            db.session.execute(text(f"UPDATE device SET currency = :c, price_per_kwh = :p WHERE {cond}"),
                               {"c": (old.get(pre + "currency") or "").strip()[:8], "p": price})
        log.info("Migration: electricity price is now set per meter")

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
