"""Background jobs (APScheduler, runs inside the single gunicorn worker)."""
import logging

from apscheduler.schedulers.background import BackgroundScheduler

from ..extensions import db
from ..models import Device, Incident, now
from . import devconfig, notify, rollup

log = logging.getLogger(__name__)
_scheduler = None


def check_disconnected():
    """'Device disconnected for X minutes' notifications."""
    for inc in Incident.query.filter_by(type="offline", ended_at=None).all():
        if (inc.detail or {}).get("notified"):
            continue
        r = notify.rule(inc.device_fk, "disconnected")
        if r is None or not r.enabled:
            continue
        minutes = r.param or devconfig.DISCONNECTED_DEFAULT_MIN
        if now() - inc.started_at >= minutes * 60:
            device = db.session.get(Device, inc.device_fk)
            if device is None or device.removed_at:
                continue
            notify.notify(device, "disconnected", f"Disconnected for more than {minutes} min")
            inc.detail = {**(inc.detail or {}), "notified": True}
    db.session.commit()


def run_rollups():
    for _ in range(20):
        if rollup.process_dirty() == 0:
            break


def start(app):
    global _scheduler
    if _scheduler is not None:
        return _scheduler

    def wrap(fn):
        def job():
            with app.app_context():
                try:
                    fn()
                except Exception:
                    log.exception("Job %s failed", fn.__name__)
                    db.session.rollback()
                finally:
                    db.session.remove()
        job.__name__ = fn.__name__
        return job

    sch = BackgroundScheduler(timezone="UTC", daemon=True)
    sch.add_job(wrap(check_disconnected), "interval", seconds=60, id="disconnected")
    sch.add_job(wrap(run_rollups), "interval", minutes=5, id="rollups")
    sch.add_job(wrap(rollup.retention), "cron", hour=3, minute=17, id="retention")
    sch.start()
    _scheduler = sch
    return sch
