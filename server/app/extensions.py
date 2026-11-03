from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from flask_login import LoginManager
from flask_sock import Sock
from flask_sqlalchemy import SQLAlchemy
from flask_wtf import CSRFProtect

db = SQLAlchemy()
login_manager = LoginManager()
csrf = CSRFProtect()
sock = Sock()
limiter = Limiter(key_func=get_remote_address, storage_uri="memory://", default_limits=[])
