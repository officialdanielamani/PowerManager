import os
import secrets


def _instance_dir():
    path = os.environ.get("POWERMANAGER_INSTANCE") or os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "instance"
    )
    os.makedirs(path, exist_ok=True)
    return path


def _secret_key(instance):
    env = os.environ.get("SECRET_KEY")
    if env:
        return env
    path = os.path.join(instance, "secret_key")
    if os.path.exists(path):
        with open(path) as f:
            return f.read().strip()
    key = secrets.token_hex(32)
    with open(path, "w") as f:
        f.write(key)
    os.chmod(path, 0o600)
    return key


class Config:
    def __init__(self):
        self.INSTANCE_DIR = _instance_dir()
        self.SECRET_KEY = _secret_key(self.INSTANCE_DIR)
        self.SQLALCHEMY_DATABASE_URI = "sqlite:///" + os.path.join(self.INSTANCE_DIR, "powermanager.db")
        # Every gunicorn thread may need a connection at the same time (SQLite connections are
        # cheap); meter WebSocket threads release theirs between messages.
        self.SQLALCHEMY_ENGINE_OPTIONS = {"connect_args": {"timeout": 15, "check_same_thread": False},
                                          "pool_size": 20, "max_overflow": 200, "pool_timeout": 10}
        self.SESSION_COOKIE_HTTPONLY = True
        self.SESSION_COOKIE_SAMESITE = "Lax"
        self.REMEMBER_COOKIE_HTTPONLY = True
        self.WTF_CSRF_TIME_LIMIT = None
        self.SETUP_TOKEN = os.environ.get("SETUP_TOKEN", "")
        self.START_SCHEDULER = True
        self.TESTING = False
