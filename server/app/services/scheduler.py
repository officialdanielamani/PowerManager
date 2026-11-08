"""Background jobs (APScheduler, runs inside the single gunicorn worker)."""
import logging

from apscheduler.schedulers.background import BackgroundScheduler

from ..extensions import db
from ..models import Device, Incident, now
from . import devconfig, notify, rollup

log = logging.getLogger(__name__)
_scheduler = None


def check_disconnected():
    """'Device disconnected for X minutes' notifications (X is chosen per user)."""
    for inc in Incident.query.filter_by(type="offline", ended_at=None).all():
        device = db.session.get(Device, inc.device_fk)
        if device is None or device.removed_at:
            continue
        done = set((inc.detail or {}).get("notified_users", []))
        newly = []
        for user, rule in notify.subscribers(device, "disconnected"):
            minutes = rule.param or devconfig.DISCONNECTED_DEFAULT_MIN
            if user.id in done or now() - inc.started_at < minutes * 60:
                continue
            notify.notify(device, "disconnected", f"Disconnected for more than {minutes} min", only_user=user)
            newly.append(user.id)
        if newly:
            inc.detail = {**(inc.detail or {}), "notified_users": sorted(done | set(newly))}
    db.session.commit()


def cleanup():
    """Per-user notification and per-meter data/event auto-delete (each only if enabled)."""
    from ..models import Event, Measurement, Notification, Rollup, Session, Temperature, User
    t = now()
    for u in User.query.filter_by(notif_autodel=True).all():
        Notification.query.filter(Notification.user_fk == u.id,
                                  Notification.ts < t - u.notif_autodel_days * 86400).delete(synchronize_session=False)
    for d in Device.query.filter((Device.data_autodel.is_(True)) | (Device.events_autodel.is_(True)) |
                                 (Device.sessions_autodel.is_(True))).all():
        if d.sessions_autodel:  # finished sessions (records only)
            cut = t - d.sessions_autodel_days * 86400
            Session.query.filter(Session.device_fk == d.id, Session.ended_at.isnot(None),
                                 Session.ended_at < cut).delete(synchronize_session=False)
        if d.data_autodel:  # Historical Data: power readings and their summaries
            cut = t - d.data_autodel_days * 86400
            Measurement.query.filter(Measurement.device_fk == d.id, Measurement.ts < cut).delete(synchronize_session=False)
            Rollup.query.filter(Rollup.device_fk == d.id, Rollup.bucket_start < cut).delete(synchronize_session=False)
            Temperature.query.filter(Temperature.device_fk == d.id, Temperature.ts < cut).delete(synchronize_session=False)
        if d.events_autodel:
            cut = t - d.events_autodel_days * 86400
            Incident.query.filter(Incident.device_fk == d.id, Incident.ended_at.isnot(None),
                                  Incident.ended_at < cut).delete(synchronize_session=False)
            Event.query.filter(Event.device_fk == d.id, Event.ts < cut).delete(synchronize_session=False)
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
    sch.add_job(wrap(cleanup), "cron", hour=3, minute=41, id="cleanup")
    sch.start()
    _scheduler = sch
    return sch
