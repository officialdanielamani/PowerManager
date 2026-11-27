#!/usr/bin/env python3
"""Development server for the browser / simulator test (run.sh): own temporary data folder, an admin account and
one pairing code, started with the Flask server (threaded, WebSocket works).

    python dev_server.py <data dir> [port]        admin / Str0ng!Passw0rd, pairing code ABCD2345
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(os.path.dirname(HERE)))   # server/

data = os.path.abspath(sys.argv[1])
port = int(sys.argv[2]) if len(sys.argv) > 2 else 5055
os.makedirs(data, exist_ok=True)
os.environ["POWERMANAGER_INSTANCE"] = data
os.environ["SETUP_TOKEN"] = "off"

from app import create_app  # noqa: E402
from app.extensions import db  # noqa: E402
from app.models import PairingCode, User, now  # noqa: E402

app = create_app({"START_SCHEDULER": False, "RATELIMIT_ENABLED": False})
with app.app_context():
    admin = User.query.filter_by(role="admin").first()
    if admin is None:
        admin = User(username="admin", role="admin", email="admin@example.com", phone="+60123456789")
        admin.set_password("Str0ng!Passw0rd")
        db.session.add(admin)
        db.session.commit()
    db.session.add(PairingCode(code="ABCD2345", expires_at=now() + 3600, created_by=admin.id))
    db.session.commit()
app.run(port=port, threaded=True)
