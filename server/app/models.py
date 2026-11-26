"""Database models. All timestamps are UTC epoch seconds (int). See docs/data-model.md."""
import secrets
import time

from flask_login import UserMixin
from werkzeug.security import check_password_hash, generate_password_hash

from .extensions import db


def now():
    return int(time.time())


class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(32), unique=True, nullable=False)  # stored lower-case
    password_hash = db.Column(db.String(255), nullable=False)
    role = db.Column(db.String(10), nullable=False, default="viewer")  # admin | manager | viewer
    # Viewer only: the manager who owns this account (null = the admin's). A manager's viewers can
    # only be linked to that manager's meters by the manager (the admin can link them anywhere).
    manager_fk = db.Column(db.Integer, db.ForeignKey("user.id"))
    email = db.Column(db.String(254), nullable=False, default="")
    phone = db.Column(db.String(16), nullable=False, default="")
    created_at = db.Column(db.Integer, nullable=False, default=now)
    last_login_at = db.Column(db.Integer)
    is_enabled = db.Column(db.Boolean, nullable=False, default=True)
    # Rotated on password change -> logs out every other browser session.
    session_token = db.Column(db.String(64), nullable=False, default=lambda: secrets.token_hex(16))
    # Viewer only: see every meter instead of only the linked ones.
    view_all = db.Column(db.Boolean, nullable=False, default=False)
    failed_logins = db.Column(db.Integer, nullable=False, default=0)
    locked_until = db.Column(db.Integer)  # epoch; -1 = locked until an admin unlocks
    telegram_chat_id = db.Column(db.String(32), nullable=False, default="")
    # Viewer permissions (admin always has both). Default off.
    can_csv = db.Column(db.Boolean, nullable=False, default=False)
    show_price = db.Column(db.Boolean, nullable=False, default=False)
    # Own notifications older than N days are deleted automatically (if enabled).
    notif_autodel = db.Column(db.Boolean, nullable=False, default=False)
    notif_autodel_days = db.Column(db.Integer, nullable=False, default=30)

    meters = db.relationship("Device", secondary="meter_viewer", lazy="dynamic",
                             backref=db.backref("viewers", lazy="dynamic"),
                             primaryjoin="User.id == MeterViewer.user_fk",
                             secondaryjoin="Device.id == MeterViewer.device_fk")

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)
        self.session_token = secrets.token_hex(16)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)

    def get_id(self):
        return f"{self.id}:{self.session_token}"

    @property
    def is_active(self):
        return self.is_enabled

    @property
    def is_admin(self):
        return self.role == "admin"

    @property
    def is_manager(self):
        return self.role == "manager"

    @property
    def is_staff(self):
        """Admin or manager: may add, configure and delete meters (a manager only its own)."""
        return self.role in ("admin", "manager")


class Setting(db.Model):
    key = db.Column(db.String(64), primary_key=True)
    value = db.Column(db.Text, nullable=False, default="")


class PairingCode(db.Model):
    code = db.Column(db.String(8), primary_key=True)
    created_at = db.Column(db.Integer, nullable=False, default=now)
    expires_at = db.Column(db.Integer, nullable=False)
    used_at = db.Column(db.Integer)
    device_fk = db.Column(db.Integer, db.ForeignKey("device.id"))
    created_by = db.Column(db.Integer, db.ForeignKey("user.id"))


class Device(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    device_id = db.Column(db.String(16), unique=True, nullable=False)
    name = db.Column(db.String(40), nullable=False)
    info = db.Column(db.String(200), nullable=False, default="")
    secret_hash = db.Column(db.String(255))
    fw = db.Column(db.String(32), default="")
    hw = db.Column(db.String(64), default="")
    hw_sig = db.Column(db.String(40))  # meter type from its firmware (devconfig.hw_signature)
    proto = db.Column(db.Integer, default=1)
    caps = db.Column(db.JSON, nullable=False, default=dict)
    paired_at = db.Column(db.Integer)
    last_seen_at = db.Column(db.Integer)
    online = db.Column(db.Boolean, nullable=False, default=False)
    cfg = db.Column(db.JSON, nullable=False, default=dict)  # UI-level config (see services/devconfig.py)
    cfg_ver_wanted = db.Column(db.Integer, nullable=False, default=1)
    cfg_ver_applied = db.Column(db.Integer, nullable=False, default=0)
    cfg_error = db.Column(db.String(200))
    last_state = db.Column(db.JSON, nullable=False, default=dict)
    last_reading = db.Column(db.JSON, nullable=False, default=dict)  # {"ts":..,"ch":[...]}
    last_seq = db.Column(db.Integer, nullable=False, default=0)
    # Incremented on every (re)pairing. The device's seq counter restarts after a factory
    # reset, so uniqueness of seq is per pairing generation.
    pair_gen = db.Column(db.Integer, nullable=False, default=1)
    removed_at = db.Column(db.Integer)      # unsynced or removed (meter factory-reset)
    purged = db.Column(db.Boolean, nullable=False, default=False)  # removed + all data deleted (tombstone)
    disabled_at = db.Column(db.Integer)     # disabled: paired but read-only, relay forced off
    # An unsynced meter paired again: the admin chooses "continue" or "new meter".
    resync_pending = db.Column(db.Boolean, nullable=False, default=False)
    # Per-meter automatic clean-up (server-only settings, not sent to the meter).
    data_autodel = db.Column(db.Boolean, nullable=False, default=False)
    data_autodel_days = db.Column(db.Integer, nullable=False, default=120)
    sessions_autodel = db.Column(db.Boolean, nullable=False, default=False)
    sessions_autodel_days = db.Column(db.Integer, nullable=False, default=120)
    events_autodel = db.Column(db.Boolean, nullable=False, default=False)
    events_autodel_days = db.Column(db.Integer, nullable=False, default=120)
    # Mesh (ESP-NOW): a leaf meter's messages travel through a gateway meter. They are signed with
    # link_key (HMAC) so the gateway can neither fake nor change them; link_up = last uplink counter.
    link_key = db.Column(db.String(64))
    link_up = db.Column(db.Integer, nullable=False, default=0)
    via_fk = db.Column(db.Integer)  # gateway (device pk) of the last mesh connection
    # Legacy (before per-channel zero / tare): not used any more, kept so old databases keep their columns.
    tare_a = db.Column(db.Float, nullable=False, default=0.0)
    tare_w = db.Column(db.Float, nullable=False, default=0.0)
    # Zero and tare per channel (docs/protocol.md §13): {"<ch>": {"zero": {"a","w","at"}, "tare": {...}, "by": user}}.
    calib = db.Column(db.JSON, nullable=False, default=dict)
    # Last sensor check per channel: {"<ch>": {"state": running|done|failed, "ref", "at", "stats", "why"}}.
    last_check = db.Column(db.JSON, nullable=False, default=dict)
    # Owner: a manager account (null = the admin). The owner manages the meter and its viewers.
    owner_fk = db.Column(db.Integer, db.ForeignKey("user.id"))
    # Electricity price of this meter (null = no cost shown). Locked into each session when it starts.
    currency = db.Column(db.String(8), nullable=False, default="")
    price_per_kwh = db.Column(db.Float, default=0.25)
    # Negative power (e.g. a clamp fitted the wrong way round) is recorded as 0.
    positive_only = db.Column(db.Boolean, nullable=False, default=True)
    # Connection choices made on the server: {"mqtt": "server" | "off" | "custom", host, port, tls, path,
    # user, pass, prefix} (custom broker). The meter gets them with the set_mqtt command.
    net = db.Column(db.JSON, nullable=False, default=dict)
    # Hardware profile (docs/protocol.md §12): rbAmp addresses, relay boards, channel table. hw_profile = wanted
    # (hw_ver = its version), hw_current = what the meter reported it is using, hw_state = queued | pending |
    # trial | active | rolled_back | rejected | "" (nothing sent yet), hw_due_at = reboot countdown end.
    hw_profile = db.Column(db.JSON, nullable=False, default=dict)
    hw_current = db.Column(db.JSON, nullable=False, default=dict)
    hw_ver = db.Column(db.Integer, nullable=False, default=0)
    hw_active_ver = db.Column(db.Integer, nullable=False, default=0)
    hw_state = db.Column(db.String(16), nullable=False, default="")
    hw_reason = db.Column(db.String(100))
    hw_due_at = db.Column(db.Integer)
    # Firmware type / hardware name the meter reports in its hello (docs/protocol.md §14) and its OTA state
    # {state: ok | trial | rolled_back, ver, why?, tried?, hw}. The partition layout / free OTA slot are in caps["ota"].
    fw_type = db.Column(db.String(24), nullable=False, default="")
    ota = db.Column(db.JSON, nullable=False, default=dict)
    # Last answer to "Get meter info".
    last_info = db.Column(db.JSON, nullable=False, default=dict)
    last_info_at = db.Column(db.Integer)

    channels = db.relationship("Channel", backref="device", cascade="all, delete-orphan",
                               order_by="Channel.idx")

    @property
    def kind(self):
        """"ac" or "dc": reported by the meter firmware (caps.kind) when pairing and on every connect."""
        return "dc" if (self.caps or {}).get("kind") == "dc" else "ac"

    @property
    def is_mc(self):
        """Multi-channel AC meter (rbAmp firmware): per-channel relays, shared / independent sessions."""
        return bool((self.caps or {}).get("mc"))

    @property
    def mc_mode(self):
        return "independent" if self.is_mc and (self.cfg or {}).get("mode") == "independent" else "shared"

    def channel_name(self, i):
        chans = (self.cfg or {}).get("ch") or []
        name = (chans[i].get("name") if i < len(chans) else "") or ""
        return f"CH{i + 1}" + (f" {name}" if name else "")

    @property
    def sensor_name(self):
        return "DC Power Sensor" if self.kind == "dc" else "AC Power Sensor"

    @property
    def synced(self):
        return self.cfg_ver_applied >= self.cfg_ver_wanted

    def set_secret(self, secret):
        self.secret_hash = generate_password_hash(secret)

    def check_secret(self, secret):
        return bool(self.secret_hash) and check_password_hash(self.secret_hash, secret)


class Channel(db.Model):
    device_fk = db.Column(db.Integer, db.ForeignKey("device.id"), primary_key=True)
    idx = db.Column(db.Integer, primary_key=True)
    label = db.Column(db.String(20), nullable=False, default="CH1")


class Measurement(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    device_fk = db.Column(db.Integer, db.ForeignKey("device.id"), nullable=False)
    ch = db.Column(db.Integer, nullable=False, default=0)
    gen = db.Column(db.Integer, nullable=False, default=1)
    seq = db.Column(db.Integer, nullable=False)
    ts = db.Column(db.Integer, nullable=False)
    v = db.Column(db.Float)
    a = db.Column(db.Float)
    w = db.Column(db.Float)
    pf = db.Column(db.Float)
    hz = db.Column(db.Float)
    e = db.Column(db.Float)
    ms = db.Column(db.Integer)  # milliseconds within ts, for sub-second readings (DRDY); else null
    time_estimated = db.Column(db.Boolean, nullable=False, default=False)

    __table_args__ = (
        db.UniqueConstraint("device_fk", "gen", "ch", "seq", name="uq_measurement_seq"),
        db.Index("ix_measurement_dev_ts", "device_fk", "ch", "ts"),
    )


class Temperature(db.Model):
    """DS18B20 readings (firmware TEMP_ENABLE): sensor idx 0 = T1 ..., recorded every TEMP_READ_MS."""
    id = db.Column(db.Integer, primary_key=True)
    device_fk = db.Column(db.Integer, db.ForeignKey("device.id"), nullable=False)
    idx = db.Column(db.Integer, nullable=False, default=0)
    ts = db.Column(db.Integer, nullable=False)
    c = db.Column(db.Float)
    rh = db.Column(db.Float)  # humidity %RH (SHT sensors), else null

    __table_args__ = (db.UniqueConstraint("device_fk", "idx", "ts", name="uq_temperature"),)


class Rollup(db.Model):
    device_fk = db.Column(db.Integer, db.ForeignKey("device.id"), primary_key=True)
    ch = db.Column(db.Integer, primary_key=True)
    period = db.Column(db.String(1), primary_key=True)  # h | d
    bucket_start = db.Column(db.Integer, primary_key=True)
    n = db.Column(db.Integer, nullable=False, default=0)
    v_min = db.Column(db.Float)
    v_max = db.Column(db.Float)
    v_avg = db.Column(db.Float)
    a_min = db.Column(db.Float)
    a_max = db.Column(db.Float)
    a_avg = db.Column(db.Float)
    w_min = db.Column(db.Float)
    w_max = db.Column(db.Float)
    w_avg = db.Column(db.Float)
    pf_avg = db.Column(db.Float)
    hz_avg = db.Column(db.Float)
    kwh = db.Column(db.Float, nullable=False, default=0.0)


class RollupDirty(db.Model):
    """Buckets that received new raw data and need their roll-up rebuilt."""
    device_fk = db.Column(db.Integer, primary_key=True)
    ch = db.Column(db.Integer, primary_key=True)
    period = db.Column(db.String(1), primary_key=True)
    bucket_start = db.Column(db.Integer, primary_key=True)


class Session(db.Model):
    __tablename__ = "meter_session"
    id = db.Column(db.Integer, primary_key=True)
    device_fk = db.Column(db.Integer, db.ForeignKey("device.id"), nullable=False)
    gen = db.Column(db.Integer, nullable=False, default=1)
    sid = db.Column(db.Integer, nullable=False)
    ch = db.Column(db.Integer)  # multi-channel meter in independent mode: the channel; None = whole meter
    started_at = db.Column(db.Integer)
    ended_at = db.Column(db.Integer)
    kwh_used = db.Column(db.Float, nullable=False, default=0.0)
    kwh_max = db.Column(db.Float, nullable=False, default=0.0)
    end_reason = db.Column(db.String(16))  # quota | expired | manual
    auto = db.Column(db.Boolean, nullable=False, default=False)
    # Electricity price locked in when the session starts (Settings -> Server -> Price).
    price_per_kwh = db.Column(db.Float)
    currency = db.Column(db.String(8))
    # Summary from the meter at session end: {"n", "v": [min, max, avg], "a": [...], "w": [...]}
    stats = db.Column(db.JSON)

    def cost(self, fallback_price=None):
        price = self.price_per_kwh if self.price_per_kwh is not None else fallback_price
        return None if price is None else round((self.kwh_used or 0) * price, 4)

    __table_args__ = (db.UniqueConstraint("device_fk", "gen", "sid", name="uq_session_sid"),)


class Event(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    device_fk = db.Column(db.Integer, db.ForeignKey("device.id"), nullable=False, index=True)
    gen = db.Column(db.Integer, nullable=False, default=1)
    seq = db.Column(db.Integer)  # null for server-made events
    ts = db.Column(db.Integer, nullable=False)
    type = db.Column(db.String(32), nullable=False)
    ch = db.Column(db.Integer)
    data = db.Column(db.JSON, nullable=False, default=dict)
    time_estimated = db.Column(db.Boolean, nullable=False, default=False)

    __table_args__ = (db.UniqueConstraint("device_fk", "gen", "seq", name="uq_event_seq"),)


class Incident(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    device_fk = db.Column(db.Integer, db.ForeignKey("device.id"), nullable=False, index=True)
    ch = db.Column(db.Integer)
    type = db.Column(db.String(10), nullable=False)  # power | offline | trip | tamper
    started_at = db.Column(db.Integer, nullable=False)
    ended_at = db.Column(db.Integer)
    duration_s = db.Column(db.Integer)
    start_event_fk = db.Column(db.Integer, db.ForeignKey("event.id"))
    end_event_fk = db.Column(db.Integer, db.ForeignKey("event.id"))
    detail = db.Column(db.JSON, nullable=False, default=dict)
    time_estimated = db.Column(db.Boolean, nullable=False, default=False)


class MeterViewer(db.Model):
    """Which view-only users may see which meter (many-to-many). Server-only: never sent to meters."""
    __tablename__ = "meter_viewer"
    device_fk = db.Column(db.Integer, db.ForeignKey("device.id", ondelete="CASCADE"), primary_key=True)
    user_fk = db.Column(db.Integer, db.ForeignKey("user.id", ondelete="CASCADE"), primary_key=True)
    # Multi-channel meters: the channel indexes this viewer may see (null = the whole meter).
    channels = db.Column(db.JSON)


class NotificationRule(db.Model):
    """Per meter AND per user: each account picks its own notifications."""
    device_fk = db.Column(db.Integer, db.ForeignKey("device.id"), primary_key=True)
    user_fk = db.Column(db.Integer, db.ForeignKey("user.id", ondelete="CASCADE"), primary_key=True)
    type = db.Column(db.String(20), primary_key=True)
    enabled = db.Column(db.Boolean, nullable=False, default=False)
    param = db.Column(db.Integer)           # threshold: % / minutes / degC, depending on the type
    channels = db.Column(db.JSON)           # multi-channel (independent): the channels this applies to (null = all)
    fired = db.Column(db.JSON, nullable=False, default=dict)  # threshold notifications already sent (re-armed when the value recovers)


class Notification(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    device_fk = db.Column(db.Integer, db.ForeignKey("device.id"), index=True)
    user_fk = db.Column(db.Integer, db.ForeignKey("user.id", ondelete="CASCADE"), index=True)
    type = db.Column(db.String(20), nullable=False)
    ts = db.Column(db.Integer, nullable=False, default=now)
    message = db.Column(db.String(300), nullable=False)
    read = db.Column(db.Boolean, nullable=False, default=False)
    telegram_status = db.Column(db.String(20))  # null | sent | failed | off


class Command(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    device_fk = db.Column(db.Integer, db.ForeignKey("device.id"), nullable=False, index=True)
    c = db.Column(db.String(20), nullable=False)
    params = db.Column(db.JSON, nullable=False, default=dict)
    created_by = db.Column(db.Integer, db.ForeignKey("user.id"))
    created_at = db.Column(db.Integer, nullable=False, default=now)
    sent_at = db.Column(db.Integer)
    acked_at = db.Column(db.Integer)
    ok = db.Column(db.Boolean)
    err = db.Column(db.String(40))
    res = db.Column(db.JSON)  # result data of the acknowledgement (i2c_scan, hw_profile ...)
    cancelled = db.Column(db.Boolean, nullable=False, default=False)


class Firmware(db.Model):
    """A firmware that can be flashed to a meter over the air or by USB from the browser (docs/protocol.md §14)."""
    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(40), nullable=False)
    info = db.Column(db.String(60), nullable=False, default="")
    description = db.Column(db.Text, nullable=False, default="")
    version = db.Column(db.String(24), nullable=False, default="")
    fw_type = db.Column(db.String(24), nullable=False, default="")   # from the firmware tag in the image
    hw = db.Column(db.String(32), nullable=False, default="")        # from the tag (e.g. esp32s3-n16r8)
    chip = db.Column(db.String(16), nullable=False, default="")      # esp32s3 ... (for the USB flash)
    size = db.Column(db.Integer, nullable=False, default=0)          # app image (OTA)
    sha256 = db.Column(db.String(64), nullable=False, default="")    # of the app image
    layout = db.Column(db.String(8), nullable=False, default="")     # hash of the partition table of the zip
    files = db.Column(db.JSON, nullable=False, default=dict)         # USB flash: {"parts": [{name, offset, size}], "flash": {...}}
    created_at = db.Column(db.Integer, nullable=False, default=now)
    updated_at = db.Column(db.Integer, nullable=False, default=now)
    updated_by = db.Column(db.String(32), nullable=False, default="")


class OtaJob(db.Model):
    """One firmware update of one meter (state follows the events of the meter)."""
    id = db.Column(db.Integer, primary_key=True)
    device_fk = db.Column(db.Integer, db.ForeignKey("device.id"), nullable=False, index=True)
    firmware_fk = db.Column(db.Integer, db.ForeignKey("firmware.id"), index=True)
    token = db.Column(db.String(64), nullable=False, unique=True)
    # pending (command sent) | downloading | verifying | rebooting | ok | failed | rolled_back
    state = db.Column(db.String(12), nullable=False, default="pending")
    pct = db.Column(db.Integer, nullable=False, default=0)
    why = db.Column(db.String(60))
    ver_from = db.Column(db.String(24), nullable=False, default="")
    ver_to = db.Column(db.String(24), nullable=False, default="")
    fw_title = db.Column(db.String(40), nullable=False, default="")
    hits = db.Column(db.Integer, nullable=False, default=0)          # downloads of the file with this token
    expires_at = db.Column(db.Integer, nullable=False, default=0)
    created_by = db.Column(db.Integer, db.ForeignKey("user.id"))
    created_at = db.Column(db.Integer, nullable=False, default=now)
    updated_at = db.Column(db.Integer, nullable=False, default=now)
