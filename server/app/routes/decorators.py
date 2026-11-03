from functools import wraps

from flask import abort
from flask_login import current_user, login_required


def admin_required(fn):
    @wraps(fn)
    @login_required
    def wrapper(*args, **kwargs):
        if not current_user.is_admin:
            abort(403)
        return fn(*args, **kwargs)
    return wrapper
