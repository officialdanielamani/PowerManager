import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import create_app  # noqa: E402
from app.extensions import db  # noqa: E402
from app.models import User  # noqa: E402
from app.services import settings  # noqa: E402

ADMIN_PW = "Str0ng!Passw0rd"


@pytest.fixture()
def app(tmp_path, monkeypatch):
    monkeypatch.setenv("POWERMANAGER_INSTANCE", str(tmp_path))
    monkeypatch.setenv("SETUP_TOKEN", "off")
    settings.clear_cache()
    app = create_app({"TESTING": True, "START_SCHEDULER": False, "WTF_CSRF_ENABLED": False,
                      "RATELIMIT_ENABLED": False})
    yield app
    settings.clear_cache()


@pytest.fixture()
def client(app):
    return app.test_client()


@pytest.fixture()
def admin(app, client):
    with app.app_context():
        u = User(username="admin", role="admin", email="a@example.com", phone="+60123456789")
        u.set_password(ADMIN_PW)
        db.session.add(u)
        db.session.commit()
    app.config["_ADMIN_EXISTS"] = True
    client.post("/login", data={"username": "ADMIN", "password": ADMIN_PW})
    return client


class FakeConn:
    def __init__(self):
        self.sent = []
        self.boot = None
        self.boot_epoch = None

    def send(self, msg):
        self.sent.append(msg)
        return len(self.sent)
