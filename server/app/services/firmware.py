"""Firmware files and over-the-air updates (docs/protocol.md §14).

The administrator uploads the zip made by the Arduino IDE. Only the files needed are kept: the app image (OTA) and
the files of a USB flash (bootloader, partition table, boot_app0, app, listed in flash_args). The firmware tag inside
the image tells the type / version / hardware; the hash of partitions.bin must match the meter's layout for OTA.
"""
import hashlib
import io
import json
import os
import re
import secrets
import shutil
import struct
import zipfile
import zlib

from flask import current_app

from ..extensions import db
from ..models import Command, Device, Firmware, OtaJob, now
from . import commands, hub, notify

MAX_ZIP = 64 * 1024 * 1024
MAX_MEMBERS = 300
TOKEN_TTL_S = 600
TOKEN_MAX_HITS = 3
STALE_S = 15 * 60       # a job that has not moved for this long is shown as lost
ACTIVE = ("pending", "downloading", "verifying", "rebooting")
_F = rb"[\x21-\x7b\x7d-\x7e]{1,39}"   # printable, no '|'
TAG_RE = re.compile(rb"PMFWTAG\|(" + _F + rb")\|(" + _F + rb")\|(" + _F + rb")\|")
FLASH_LINE = re.compile(r"^(0x[0-9a-fA-F]+)\s+(\S+)\s*$")


def root():
    d = os.path.join(current_app.config["INSTANCE_DIR"], "firmware")
    os.makedirs(d, exist_ok=True)
    return d


def folder(fid):
    return os.path.join(root(), str(int(fid)))


# ---------------------------------------------------------------- the zip

def scan_tag(data):
    """(type, version, hw) from the firmware tag of an image, or None."""
    m = TAG_RE.search(data)
    return tuple(g.decode() for g in m.groups()) if m else None


def layout_hash(table):
    """Hash of a partition table (partitions.bin): type, subtype, offset, size of every entry, CRC-32 (as the meter)."""
    crc = 0
    n = 0
    for i in range(0, len(table) - 31, 32):
        magic, ptype, subtype, offset, size = struct.unpack_from("<HBBII", table, i)
        if magic != 0x50AA:      # 0xAA50 stored little endian: end / MD5 entry
            break
        crc = zlib.crc32(struct.pack("<BBII", ptype, subtype, offset, size), crc)
        n += 1
    return ("%08x" % (crc & 0xFFFFFFFF)) if n else ""


def parse_flash_args(text):
    """flash_args of the Arduino build: options and "offset file" lines -> ({mode, freq, size}, [(offset, file)])."""
    opts, parts = {}, []
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("--"):
            toks = line.split()
            for k, v in zip(toks[::2], toks[1::2]):
                opts[k.lstrip("-")] = v
            continue
        m = FLASH_LINE.match(line)
        if m:
            parts.append((int(m.group(1), 16), m.group(2)))
    flash = {"mode": opts.get("flash_mode", "dio"), "freq": opts.get("flash_freq", "80m"), "size": opts.get("flash_size", "keep")}
    return flash, parts


def parse_zip(blob):
    try:
        return _parse_zip(blob)
    except (zipfile.BadZipFile, zlib.error, EOFError, NotImplementedError, RuntimeError):
        return None, None, "The zip is damaged or uses an unsupported format."


def _parse_zip(blob):
    """Read the uploaded zip. Returns (info, files, error): info = fields of the Firmware row, files = {name: bytes} to keep."""
    if len(blob) > MAX_ZIP:
        return None, None, "The zip is too big (max %d MB)." % (MAX_ZIP // 1048576)
    try:
        zf = zipfile.ZipFile(io.BytesIO(blob))
    except zipfile.BadZipFile:
        return None, None, "This is not a zip file."
    infos = [i for i in zf.infolist() if not i.is_dir()]
    if not infos or len(infos) > MAX_MEMBERS or sum(i.file_size for i in infos) > MAX_ZIP * 2:
        return None, None, "The zip is empty or has too many / too big files."
    members = {}
    for i in infos:  # only the file name counts (no paths: zip-slip)
        base = os.path.basename(i.filename.replace("\\", "/"))
        if base and not base.startswith(".") and base not in members:
            members[base] = i
    apps = [n for n in members if n.endswith(".ino.bin") or (n.endswith(".bin") and not any(
        x in n for x in ("bootloader", "partitions", "merged", "boot_app0")))]
    apps = [n for n in apps if not any(x in n for x in ("bootloader", "partitions", "merged"))]
    if len(apps) != 1:
        return None, None, "The zip needs exactly one app image (<sketch>.ino.bin); found: %s." % (", ".join(apps) or "none")
    app = zf.read(members[apps[0]])
    if len(app) < 4096 or app[0] != 0xE9:
        return None, None, "%s is not an ESP32 app image." % apps[0]
    tag = scan_tag(app)
    if not tag:
        return None, None, "No PowerManager firmware tag in the image: this is not a PowerManager firmware (or too old)."
    fw_type, version, hw = tag
    layout = ""
    pname = next((n for n in members if n.endswith(".partitions.bin") or n == "partitions.bin"), None)
    if pname:
        layout = layout_hash(zf.read(members[pname]))
    files = {apps[0]: app}
    flash = {"mode": "dio", "freq": "80m", "size": "keep"}
    parts = []
    if "flash_args" in members:
        flash, listed = parse_flash_args(zf.read(members["flash_args"]).decode("utf-8", "replace"))
        for off, name in listed:
            base = os.path.basename(name)
            if base in members:
                parts.append((off, base))
    elif pname:  # no flash_args: the standard ESP32-S3 offsets
        for off, name in ((0x0, next((n for n in members if n.endswith(".bootloader.bin")), None)), (0x8000, pname),
                          (0xE000, "boot_app0.bin" if "boot_app0.bin" in members else None), (0x10000, apps[0])):
            if name:
                parts.append((off, name))
    if not any(n == apps[0] for _, n in parts):
        parts = []  # without the app in the list there is nothing to flash by USB (merged image as fallback)
    merged = next((n for n in members if n.endswith(".merged.bin")), None)
    if not parts and merged:
        parts = [(0x0, merged)]
    for _, name in parts:
        if name not in files:
            files[name] = zf.read(members[name])
    chip = ""
    if "build.options.json" in members:
        try:
            m = re.search(r"esp32:esp32:(\w+)", json.loads(zf.read(members["build.options.json"]).decode("utf-8", "replace")).get("fqbn", ""))
            chip = m.group(1) if m else ""
        except ValueError:
            pass
    if not chip:
        chip = "esp32s3" if "s3" in hw else "esp32"
    info = {"version": version[:24], "fw_type": fw_type[:24], "hw": hw[:32], "chip": chip[:16], "size": len(app),
            "sha256": hashlib.sha256(app).hexdigest(), "layout": layout,
            "files": {"app": apps[0], "flash": flash,
                      "parts": [{"offset": off, "name": name, "size": len(files[name])} for off, name in sorted(parts)]}}
    return info, files, None


def store(fw, files):
    """Write the kept files of a firmware (replaces the old ones)."""
    d = folder(fw.id)
    shutil.rmtree(d, ignore_errors=True)
    os.makedirs(d, exist_ok=True)
    for name, data in files.items():
        with open(os.path.join(d, name), "wb") as f:
            f.write(data)


def remove_files(fid):
    shutil.rmtree(folder(fid), ignore_errors=True)


def file_path(fw, name):
    """Absolute path of a kept file of this firmware (only names that belong to it)."""
    allowed = {p["name"] for p in (fw.files or {}).get("parts", [])} | {(fw.files or {}).get("app")}
    if name not in allowed:
        return None
    p = os.path.join(folder(fw.id), name)
    return p if os.path.isfile(p) else None


# ---------------------------------------------------------------- compare firmware and meter

def _ver_tuple(v):
    m = re.match(r"(\d+)\.(\d+)\.(\d+)", v or "")
    return tuple(int(x) for x in m.groups()) if m else None


def meter_view(device):
    caps_ota = (device.caps or {}).get("ota") or {}
    sessions = [s for s in ((device.last_state or {}).get("sessions") or []) if isinstance(s, dict)]
    if not sessions and isinstance((device.last_state or {}).get("session"), dict):
        sessions = [device.last_state["session"]]
    running = any(s.get("status") in (None, "active", 0, "running") and s.get("sid") for s in sessions)
    return {"id": device.id, "name": device.name, "device_id": device.device_id, "online": hub.is_online(device.id),
            "fw": device.fw, "fw_type": device.fw_type, "hw": (device.ota or {}).get("hw", ""),
            "slot": caps_ota.get("slot"), "layout": caps_ota.get("layout"), "ota": bool(caps_ota),
            "ota_state": (device.ota or {}).get("state", ""), "sessions_running": running,
            "channels": (device.caps or {}).get("channels", 1)}


def verdict(fw, device):
    """What stops the update (blocked) and what only deserves a warning."""
    m = meter_view(device)
    blocked, warnings = [], []
    if not m["online"]:
        blocked.append("The meter is offline.")
    if not m["ota"]:
        blocked.append("This meter's firmware cannot be updated over the air yet: flash it once by USB.")
    else:
        if m["fw_type"] and m["fw_type"] != fw.fw_type:
            blocked.append(f"The firmware type differs ({m['fw_type']} on the meter, {fw.fw_type} in the file).")
        if m["layout"] and fw.layout and m["layout"] != fw.layout:
            blocked.append("The partition layout differs: this needs a USB flash.")
        if m["slot"] and fw.size > m["slot"]:
            blocked.append("The firmware is bigger than the free update space of the meter.")
    if not m["fw_type"] and m["ota"]:
        blocked.append("The meter did not report its firmware type.")
    job = active_job(device)
    if job:
        blocked.append("A firmware update of this meter is already running.")
    a, b = _ver_tuple(m["fw"]), _ver_tuple(fw.version)
    if a and b and b < a:
        warnings.append(f"This is an older version ({fw.version}) than the one on the meter ({m['fw']}).")
    elif a and b and a == b:
        warnings.append(f"The meter already runs {m['fw']}.")
    if m["hw"] and fw.hw and m["hw"] != fw.hw:
        warnings.append(f"The hardware name differs ({m['hw']} on the meter, {fw.hw} in the file).")
    if m["sessions_running"]:
        warnings.append("A session is running: it ends when the update starts.")
    warnings.append("The relays switch off during the update and the restart.")
    return {"meter": m, "blocked": blocked, "warnings": warnings}


# ---------------------------------------------------------------- jobs

def active_job(device):
    job = (OtaJob.query.filter_by(device_fk=device.id).filter(OtaJob.state.in_(ACTIVE))
           .order_by(OtaJob.id.desc()).first())
    if job and now() - job.updated_at > STALE_S:
        job.state, job.why = "failed", "lost"
        db.session.commit()
        return None
    return job


def start(fw, device, user):
    """Create the job and send the command. Returns (job, error text)."""
    v = verdict(fw, device)
    if v["blocked"]:
        return None, v["blocked"][0]
    job = OtaJob(device_fk=device.id, firmware_fk=fw.id, token=secrets.token_urlsafe(24), state="pending",
                 ver_from=device.fw or "", ver_to=fw.version, fw_title=fw.title,
                 expires_at=now() + TOKEN_TTL_S, created_by=user.id if user else None)
    db.session.add(job)
    db.session.flush()
    params = {"path": f"/api/v1/ota/{job.token}", "size": fw.size, "sha256": fw.sha256, "ver": fw.version, "type": fw.fw_type}
    cmd, err = commands.issue(device, "ota", params, user)
    if err:
        db.session.rollback()
        return None, {"offline": "The meter is offline."}.get(err, err)
    return job, None


def job_view(job):
    if job is None:
        return None
    stale = job.state in ACTIVE and now() - job.updated_at > STALE_S
    return {"id": job.id, "state": "failed" if stale else job.state, "pct": job.pct, "why": "lost" if stale else job.why,
            "ver_from": job.ver_from, "ver_to": job.ver_to, "title": job.fw_title, "at": job.created_at}


def download(token):
    """The file of a job for the meter (one-time token: a few tries within the time limit). Returns (job, fw) or (None, None)."""
    job = OtaJob.query.filter_by(token=token).first()
    if job is None or job.state not in ("pending", "downloading") or job.expires_at < now() or job.hits >= TOKEN_MAX_HITS:
        return None, None
    fw = db.session.get(Firmware, job.firmware_fk) if job.firmware_fk else None
    if fw is None:
        return None, None
    job.hits += 1
    job.updated_at = now()
    db.session.commit()
    return job, fw


# ---------------------------------------------------------------- meter -> server

def on_hello(device, msg):
    """fw_type / OTA state of the hello. A rolled back update (or an update that has just come back) closes its job."""
    if "fw_type" in msg:
        device.fw_type = str(msg.get("fw_type") or "")[:24]
    o = msg.get("ota")
    if isinstance(o, dict):
        state = {k: o[k] for k in ("state", "ver", "why", "tried") if k in o}
        if msg.get("hw"):
            state["hw"] = str(msg["hw"])[:32]
        device.ota = state
        job = (OtaJob.query.filter_by(device_fk=device.id).filter(OtaJob.state.in_(ACTIVE))
               .order_by(OtaJob.id.desc()).first())
        if job and job.state in ("rebooting", "downloading", "verifying"):
            if o.get("state") == "rolled_back":
                job.state, job.why, job.updated_at = "rolled_back", str(o.get("why") or "")[:60], now()
                notify.notify(device, "reboot_pending",
                              f"Firmware update to {job.ver_to} rolled back ({o.get('why') or 'unknown'}): the meter runs {device.fw} again")
            elif o.get("state") == "trial" and device.fw == job.ver_to:
                job.state, job.pct, job.updated_at = "rebooting", 100, now()   # back with the new firmware, waiting for confirm


def on_event(device, ev):
    """ota_progress / ota_failed / ota_ok events."""
    d = ev.data or {}
    job = (OtaJob.query.filter_by(device_fk=device.id).filter(OtaJob.state.in_(ACTIVE))
           .order_by(OtaJob.id.desc()).first())
    if job is None:
        job = OtaJob.query.filter_by(device_fk=device.id).order_by(OtaJob.id.desc()).first()
    if job is None:
        return
    job.updated_at = now()
    if ev.type == "ota_progress":
        st = str(d.get("state") or "")
        if st in ("downloading", "verifying", "rebooting") and job.state in ACTIVE:
            job.state = st
            job.pct = int(d.get("pct") or 0)
            if st == "rebooting":
                notify.notify(device, "reboot_pending", f"Meter restarts now to install firmware {job.ver_to}")
    elif ev.type == "ota_failed":
        job.state, job.why = "failed", str(d.get("why") or "unknown")[:60]
        notify.notify(device, "reboot_pending", f"Firmware update to {job.ver_to} failed: {job.why}")
    elif ev.type == "ota_ok":
        job.state, job.pct, job.why = "ok", 100, None
        notify.notify(device, "reboot_pending", f"Meter updated to firmware {job.ver_to}")


def on_cmd_ack(device, cmd):
    """The meter refused the ota command (wrong type, too big, busy ...)."""
    if cmd.c != "ota" or cmd.ok:
        return
    job = (OtaJob.query.filter_by(device_fk=device.id, state="pending").order_by(OtaJob.id.desc()).first())
    if job:
        job.state, job.why, job.updated_at = "failed", (cmd.err or "refused")[:60], now()
