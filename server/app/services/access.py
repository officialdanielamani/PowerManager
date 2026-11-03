"""Who can see which meter. Admin: all. Viewer: linked meters, or all if `view_all` is set."""
from flask import abort
from flask_login import current_user

from ..extensions import db
from ..models import Device, MeterViewer


def can_view(user, device):
    if user is None or not user.is_authenticated:
        return False
    if user.is_admin or user.view_all:
        return True
    return db.session.get(MeterViewer, (device.id, user.id)) is not None


def visible_devices(user, removed=False):
    q = Device.query.filter(Device.removed_at.isnot(None) if removed else Device.removed_at.is_(None))
    if not (user.is_admin or user.view_all):
        q = q.join(MeterViewer, MeterViewer.device_fk == Device.id).filter(MeterViewer.user_fk == user.id)
    return q.order_by(Device.name)


def device_for_current_user(pk, allow_removed=False):
    d = db.session.get(Device, pk)
    if d is None or (d.removed_at and not allow_removed) or not can_view(current_user, d):
        abort(404)  # 404, not 403: do not reveal that the meter exists
    return d
