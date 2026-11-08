"""Who can see / manage which meter and which accounts.

- admin: every meter and every account; server settings.
- manager: the meters it owns (Device.owner_fk) and the view-only accounts it created
  (User.manager_fk). It adds, configures and deletes its own meters.
- viewer: meters linked to it (MeterViewer, optionally limited to some channels). "View all"
  means every meter of its manager (or every meter, for the admin's own viewers).
"""
from flask import abort
from flask_login import current_user
from sqlalchemy import or_, select

from ..extensions import db
from ..models import Device, MeterViewer, User


def _authed(user):
    return user is not None and user.is_authenticated


def can_manage(user, device):
    """Configure, control, delete data / the meter, link viewers."""
    if not _authed(user):
        return False
    return user.is_admin or (user.is_manager and device.owner_fk == user.id)


def can_view(user, device):
    if not _authed(user):
        return False
    if user.is_admin:
        return True
    if user.is_manager:
        return device.owner_fk == user.id
    if user.view_all and (user.manager_fk is None or device.owner_fk == user.manager_fk):
        return True
    return db.session.get(MeterViewer, (device.id, user.id)) is not None


def allowed_channels(user, device):
    """None = the whole meter; else the set of channel indexes this user may see."""
    if not _authed(user) or user.is_staff or not device.is_mc:
        return None
    if user.view_all and (user.manager_fk is None or device.owner_fk == user.manager_fk):
        return None
    link = db.session.get(MeterViewer, (device.id, user.id))
    if link is None or not link.channels:
        return None
    return {int(c) for c in link.channels if isinstance(c, int) or str(c).isdigit()}


def visible_devices(user, removed=False):
    q = Device.query.filter(Device.removed_at.isnot(None) if removed else Device.removed_at.is_(None),
                            Device.purged.is_(False))
    if user.is_admin:
        pass
    elif user.is_manager:
        q = q.filter(Device.owner_fk == user.id)
    else:
        linked = Device.id.in_(select(MeterViewer.device_fk).where(MeterViewer.user_fk == user.id))
        if user.view_all and user.manager_fk is None:
            pass
        elif user.view_all:
            q = q.filter(or_(Device.owner_fk == user.manager_fk, linked))
        else:
            q = q.filter(linked)
    return q.order_by(Device.name)


def managed_devices(user, removed=False):
    """Meters this user may manage (admin: all, manager: its own, viewer: none)."""
    if not _authed(user) or not user.is_staff:
        return Device.query.filter(db.false())
    return visible_devices(user, removed)


def device_for_current_user(pk, allow_removed=False):
    d = db.session.get(Device, pk)
    if d is None or d.purged or (d.removed_at and not allow_removed) or not can_view(current_user, d):
        abort(404)
    return d


def device_for_manager(pk, allow_removed=False):
    """Management pages: 404 when the meter is not visible, 403 when visible but not managed."""
    d = device_for_current_user(pk, allow_removed)
    if not can_manage(current_user, d):
        abort(403)
    return d


def managed_users(user):
    """Accounts this user may manage: admin -> every manager and viewer; manager -> its viewers."""
    if not _authed(user) or not user.is_staff:
        return User.query.filter(db.false())
    if user.is_admin:
        return User.query.filter(User.role.in_(("manager", "viewer")))
    return User.query.filter(User.role == "viewer", User.manager_fk == user.id)


def can_manage_user(actor, user):
    if user is None or not _authed(actor):
        return False
    if actor.is_admin:
        return user.role in ("manager", "viewer")
    return actor.is_manager and user.role == "viewer" and user.manager_fk == actor.id


def managers():
    return User.query.filter_by(role="manager").order_by(User.username).all()


def can_csv(user):
    """Download .csv files: staff always, viewers only when enabled for them."""
    return bool(_authed(user) and (user.is_staff or user.can_csv))


def can_price(user):
    """See electricity prices / costs: staff always, viewers only when enabled for them."""
    return bool(_authed(user) and (user.is_staff or user.show_price))
