"""Small in-place schema upgrades for SQLite (the app uses create_all, which never alters tables). Databases of 1.0.0 are
upgraded; older ones are not supported.

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
    """Upgrade a 1.0 database."""
    _add_column("notification_rule", "channels JSON")
    _add_column("notification_rule", "fired JSON NOT NULL DEFAULT '{}'")
    # firmware type / OTA state of the meter (docs/protocol.md §14)
    _add_column("device", "fw_type VARCHAR(24) NOT NULL DEFAULT ''")
    _add_column("device", "ota JSON NOT NULL DEFAULT '{}'")
    # hardware profile sent from the server, command results
    for ddl in ("hw_profile JSON NOT NULL DEFAULT '{}'", "hw_current JSON NOT NULL DEFAULT '{}'",
                "hw_ver INTEGER NOT NULL DEFAULT 0", "hw_active_ver INTEGER NOT NULL DEFAULT 0",
                "hw_state VARCHAR(16) NOT NULL DEFAULT ''", "hw_reason VARCHAR(100)", "hw_due_at INTEGER"):
        _add_column("device", ddl)
    _add_column("command", "res JSON")
    # zero / tare per channel, sensor checks
    _add_column("device", "calib JSON NOT NULL DEFAULT '{}'")
    _add_column("device", "last_check JSON NOT NULL DEFAULT '{}'")
    db.session.commit()
