"""Firmware files and OTA (docs/protocol.md §14): the zip, the tag, the layout hash, list / add / delete, the flash check,
the update job (command, one-time download, events, rollback), notifications, USB files and access."""
import io
import struct
import zipfile

import pytest

from app.extensions import db
from app.models import Device, Firmware, Notification, NotificationRule, OtaJob, User, now
from app.services import firmware as fws, hub, ingest

from .conftest import ADMIN_PW, FakeConn
from .test_access import VPW, _login, _meter

MARK = b"PMFW" b"TAG|"
LAYOUT = "3e58d8a2"   # firmware/tools/ota_test: the layout of firmware/PowerMeterRBAmp/partitions.csv, as the meter hashes it


def partitions_bin(entries=None):
    entries = entries or [(1, 2, 0x9000, 0x5000), (1, 0, 0xE000, 0x2000), (0, 0x10, 0x10000, 0x600000),
                          (0, 0x11, 0x610000, 0x600000), (1, 0x82, 0xC10000, 0x3E0000), (1, 3, 0xFF0000, 0x10000)]
    out = b""
    for t, st, off, size in entries:
        out += struct.pack("<HBBII16sI", 0x50AA, t, st, off, size, b"x", 0)
    return out + b"\xff" * 32


def make_zip(ver="1.3.0", fw_type="rbamp-mc", hw="esp32s3-n16r8", tag=True, parts=None, app_size=6000, extra=None, magic=b"\xe9"):
    app = magic + b"\x01" * 100 + (MARK + f"{fw_type}|{ver}|{hw}|".encode() if tag else b"") + b"\x02" * app_size
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("PowerMeterRBAmp.ino.bin", app)
        z.writestr("PowerMeterRBAmp.ino.bootloader.bin", b"\xe9boot" * 50)
        z.writestr("PowerMeterRBAmp.ino.partitions.bin", partitions_bin(parts))
        z.writestr("boot_app0.bin", b"\x01" * 64)
        z.writestr("PowerMeterRBAmp.ino.merged.bin", b"\xff" * 20000)
        z.writestr("PowerMeterRBAmp.ino.elf", b"ELF" * 1000)
        z.writestr("build.options.json", '{"fqbn": "esp32:esp32:esp32s3:CDCOnBoot=cdc"}')
        z.writestr("flash_args", "--flash_mode dio --flash_freq 80m --flash_size 16MB\n0x0 PowerMeterRBAmp.ino.bootloader.bin\n"
                                 "0x8000 PowerMeterRBAmp.ino.partitions.bin\n0xe000 boot_app0.bin\n0x10000 PowerMeterRBAmp.ino.bin\n")
        for n, b in (extra or {}).items():
            z.writestr(n, b)
    return buf.getvalue()


def upload(admin, blob=None, title="RB-Amp 16C Relay", **kw):
    data = {"title": title, "info": "7C RB-Amp", "description": "text", "version": kw.get("version", ""),
            "zip": (io.BytesIO(blob if blob is not None else make_zip()), "fw.zip")}
    return admin.post("/firmware/add", data=data, content_type="multipart/form-data")


@pytest.fixture()
def online(monkeypatch):
    sent = []
    monkeypatch.setattr(hub, "is_online", lambda pk: True)
    monkeypatch.setattr(hub, "send", lambda pk, msg: sent.append(msg) or 1)
    return sent


def ota_meter(app, name="Dryer A", did="AAAA1111", **over):
    with app.app_context():
        pk = _meter(name, did)
        d = db.session.get(Device, pk)
        d.fw, d.fw_type = "1.1.0", "rbamp-mc"
        d.caps = {"channels": 1, "ota": {"slot": 6291456, "layout": LAYOUT, "tag": "x"}}
        d.ota = {"state": "ok", "ver": "1.1.0", "hw": "esp32s3-n16r8"}
        for k, v in over.items():
            setattr(d, k, v)
        db.session.commit()
    return pk


# ------------------------------------------------------------------ the zip

def test_parse_zip():
    info, files, err = fws.parse_zip(make_zip())
    assert err is None
    assert (info["version"], info["fw_type"], info["hw"], info["chip"]) == ("1.3.0", "rbamp-mc", "esp32s3-n16r8", "esp32s3")
    assert info["layout"] == LAYOUT
    assert [(p["offset"], p["name"]) for p in info["files"]["parts"]] == [
        (0, "PowerMeterRBAmp.ino.bootloader.bin"), (0x8000, "PowerMeterRBAmp.ino.partitions.bin"), (0xE000, "boot_app0.bin"),
        (0x10000, "PowerMeterRBAmp.ino.bin")]
    assert info["files"]["flash"] == {"mode": "dio", "freq": "80m", "size": "16MB"}
    assert set(files) == {"PowerMeterRBAmp.ino.bin", "PowerMeterRBAmp.ino.bootloader.bin", "PowerMeterRBAmp.ino.partitions.bin", "boot_app0.bin"}
    assert len(info["sha256"]) == 64 and info["size"] == len(files["PowerMeterRBAmp.ino.bin"])


def test_bad_zips():
    assert "not a zip" in fws.parse_zip(b"hello")[2]
    assert "firmware tag" in fws.parse_zip(make_zip(tag=False))[2]
    assert "not an ESP32 app image" in fws.parse_zip(make_zip(magic=b"\x00"))[2]
    both = make_zip(extra={"Other.ino.bin": b"\xe9" * 5000})
    assert "exactly one app image" in fws.parse_zip(both)[2]
    assert fws.layout_hash(partitions_bin()) == LAYOUT
    assert fws.layout_hash(partitions_bin([(0, 0x10, 0x10000, 0x100000)])) != LAYOUT
    assert fws.scan_tag(b"xx" + MARK + b"a|b|c|yy") == ("a", "b", "c")
    assert fws.scan_tag(MARK + b"a|b|") is None


# ------------------------------------------------------------------ list / add / delete / info

def test_add_list_info_delete(app, admin):
    assert upload(admin).status_code == 302
    page = admin.get("/firmware").get_data(as_text=True)
    assert "RB-Amp 16C Relay" in page and "7C RB-Amp" in page and "rbamp-mc" in page and "1.3.0" in page and "Flash" in page
    with app.app_context():
        fw = Firmware.query.one()
        fid, uid = fw.id, fw.updated_by
        assert uid == "admin" and fw.layout == LAYOUT
    info = admin.get(f"/firmware/{fid}").get_data(as_text=True)
    assert "text" in info and "esp32s3-n16r8" in info and "Updated by" in info
    assert upload(admin, blob=b"junk").status_code == 200   # error shown, nothing added
    assert admin.post(f"/firmware/{fid}/edit", data={"title": "New title", "info": "i", "description": "d", "version": "9.9.9"}).status_code == 302
    with app.app_context():
        assert Firmware.query.one().title == "New title" and Firmware.query.one().version == "9.9.9"
    assert admin.post("/firmware/delete", data={"ids": [str(fid)]}).status_code == 302
    with app.app_context():
        assert Firmware.query.count() == 0


def test_edit_rejects_other_type(app, admin):
    upload(admin)
    with app.app_context():
        fid = Firmware.query.one().id
    r = admin.post(f"/firmware/{fid}/edit", data={"title": "t", "zip": (io.BytesIO(make_zip(fw_type="pzem-ac")), "x.zip")},
                   content_type="multipart/form-data")
    assert r.status_code == 200 and b"different firmware type" in r.data


def test_access(app, admin):
    upload(admin)
    with app.app_context():
        fid = Firmware.query.one().id
        u = User(username="mgr", role="manager", email="m@example.com", phone="+60123456780")
        u.set_password(VPW)
        db.session.add(u)
        db.session.commit()
    m = _login(app, "mgr")
    assert m.get("/firmware").status_code == 200
    page = m.get("/firmware").get_data(as_text=True)
    assert "Add New Firmware" not in page and "Delete selected" not in page
    assert m.get(f"/firmware/{fid}").status_code == 200 and m.get(f"/firmware/{fid}/flash").status_code == 200
    assert m.get("/firmware/add").status_code == 403 and m.get(f"/firmware/{fid}/edit").status_code == 403
    assert m.post("/firmware/delete", data={"ids": [str(fid)]}).status_code == 403
    with app.app_context():
        _meter("Viewer meter", "VVVV0001")
        _viewer = User(username="vw", role="viewer", email="v@example.com", phone="+60123456781")
        _viewer.set_password(VPW)
        db.session.add(_viewer)
        db.session.commit()
    assert _login(app, "vw").get("/firmware").status_code == 403


# ------------------------------------------------------------------ the check and the update

def test_verdict(app, admin, online):
    upload(admin)
    pk = ota_meter(app)
    with app.app_context():
        fw = Firmware.query.one()
        d = db.session.get(Device, pk)
        v = fws.verdict(fw, d)
        assert not v["blocked"] and any("relays switch off" in w for w in v["warnings"])
        d.caps = {"channels": 1}                              # firmware without OTA support
        assert any("flash it once by USB" in b for b in fws.verdict(fw, d)["blocked"])
        d.caps = {"channels": 1, "ota": {"slot": 6291456, "layout": "deadbeef"}}
        assert any("partition layout" in b for b in fws.verdict(fw, d)["blocked"])
        d.caps = {"channels": 1, "ota": {"slot": 1000, "layout": LAYOUT}}
        assert any("bigger" in b for b in fws.verdict(fw, d)["blocked"])
        d.caps = {"channels": 1, "ota": {"slot": 6291456, "layout": LAYOUT}}
        d.fw_type = "pzem-ac"
        assert any("type differs" in b for b in fws.verdict(fw, d)["blocked"])
        d.fw_type, d.fw = "rbamp-mc", "2.0.0"
        assert any("older version" in w for w in fws.verdict(fw, d)["warnings"])
    page = admin.get(f"/firmware/{fw.id}/check?meter={pk}").get_json()
    assert page["ok"] and page["meter"]["name"] == "Dryer A"


def test_offline_is_blocked(app, admin):
    upload(admin)
    pk = ota_meter(app)
    with app.app_context():
        v = fws.verdict(Firmware.query.one(), db.session.get(Device, pk))
    assert any("offline" in b for b in v["blocked"])


def test_ota_flow(app, admin, online):
    upload(admin)
    pk = ota_meter(app)
    with app.app_context():
        fid = Firmware.query.one().id
        sha = Firmware.query.one().sha256
    assert admin.post(f"/firmware/{fid}/ota", json={"meter": pk}).status_code == 400   # needs the confirmation
    r = admin.post(f"/firmware/{fid}/ota", json={"meter": pk, "confirm": True})
    assert r.status_code == 200 and r.get_json()["job"]["state"] == "pending"
    cmd = online[-1]
    assert cmd["c"] == "ota" and cmd["size"] > 0 and cmd["sha256"] == sha and cmd["ver"] == "1.3.0" and cmd["type"] == "rbamp-mc"
    assert cmd["path"].startswith("/api/v1/ota/")
    assert admin.post(f"/firmware/{fid}/ota", json={"meter": pk, "confirm": True}).status_code == 409   # one at a time
    # the meter downloads the file with the one-time token (no login)
    dl = app.test_client().get(cmd["path"])
    assert dl.status_code == 200 and dl.data[:1] == b"\xe9" and len(dl.data) == cmd["size"]
    for _ in range(2):
        assert app.test_client().get(cmd["path"]).status_code == 200
    assert app.test_client().get(cmd["path"]).status_code == 404           # at most 3 downloads
    assert app.test_client().get("/api/v1/ota/nope").status_code == 404
    # events of the meter
    with app.app_context():
        d = db.session.get(Device, pk)
        job = OtaJob.query.one()

        def evt(e, **kw):
            ingest.handle(FakeConn(), d, dict({"t": "evt", "seq": evt.n, "ts": now(), "tv": 1, "e": e}, **kw))
            evt.n += 1
            db.session.commit()
        evt.n = 1
        evt("ota_progress", state="downloading", pct=40, ver="1.3.0")
        assert job.state == "downloading" and job.pct == 40
        evt("ota_progress", state="rebooting", pct=100, ver="1.3.0")
        assert job.state == "rebooting"
        assert any("restarts now" in n.message for n in Notification.query.all())
        # back with the new firmware, confirmed
        ingest.handle(FakeConn(), d, {"t": "hello", "fw": "1.3.0", "fw_type": "rbamp-mc", "hw": "esp32s3-n16r8", "proto": 1,
                                      "ota": {"state": "trial", "ver": "1.3.0"}, "caps": {"channels": 1, "ota": {"slot": 1, "layout": LAYOUT}}})
        evt("ota_ok", state="ok", pct=100, ver="1.3.0")
        db.session.refresh(job)
        assert job.state == "ok" and d.fw == "1.3.0" and d.ota["state"] == "trial"
        assert any("updated to firmware 1.3.0" in n.message for n in Notification.query.all())
    st = admin.get(f"/firmware/job/{job.id}").get_json()
    assert st["job"]["state"] == "ok"


def test_ota_failed_and_rollback(app, admin, online):
    upload(admin)
    pk = ota_meter(app)
    with app.app_context():
        fid = Firmware.query.one().id
    admin.post(f"/firmware/{fid}/ota", json={"meter": pk, "confirm": True})
    with app.app_context():
        d = db.session.get(Device, pk)
        ingest.handle(FakeConn(), d, {"t": "evt", "seq": 1, "ts": now(), "tv": 1, "e": "ota_failed", "state": "failed", "why": "sha256", "ver": "1.3.0"})
        db.session.commit()
        job = OtaJob.query.one()
        assert job.state == "failed" and job.why == "sha256"
        assert any("failed: sha256" in n.message for n in Notification.query.all())
    admin.post(f"/firmware/{fid}/ota", json={"meter": pk, "confirm": True})
    with app.app_context():
        d = db.session.get(Device, pk)
        ingest.handle(FakeConn(), d, {"t": "evt", "seq": 2, "ts": now(), "tv": 1, "e": "ota_progress", "state": "rebooting", "pct": 100, "ver": "1.3.0"})
        ingest.handle(FakeConn(), d, {"t": "hello", "fw": "1.1.0", "fw_type": "rbamp-mc", "proto": 1,
                                      "ota": {"state": "rolled_back", "ver": "1.1.0", "tried": "1.3.0", "why": "no_server"},
                                      "caps": {"channels": 1, "ota": {"slot": 1, "layout": LAYOUT}}})
        db.session.commit()
        job = OtaJob.query.order_by(OtaJob.id.desc()).first()
        assert job.state == "rolled_back" and job.why == "no_server"
        assert any("rolled back" in n.message for n in Notification.query.all())


def test_refused_by_the_meter(app, admin, online):
    upload(admin)
    pk = ota_meter(app)
    with app.app_context():
        fid = Firmware.query.one().id
    admin.post(f"/firmware/{fid}/ota", json={"meter": pk, "confirm": True})
    with app.app_context():
        d = db.session.get(Device, pk)
        from app.models import Command
        cmd = Command.query.filter_by(c="ota").one()
        ingest.handle(FakeConn(), d, {"t": "cmd_ack", "ref": cmd.id, "ok": False, "err": "too_big"})
        db.session.commit()
        assert OtaJob.query.one().state == "failed" and OtaJob.query.one().why == "too_big"


def test_stale_job_is_lost(app, admin, online):
    upload(admin)
    pk = ota_meter(app)
    with app.app_context():
        fw = Firmware.query.one()
        d = db.session.get(Device, pk)
        job, err = fws.start(fw, d, None)
        assert err is None
        job.updated_at = now() - 3600
        db.session.commit()
        assert fws.active_job(d) is None and OtaJob.query.one().state == "failed" and OtaJob.query.one().why == "lost"


# ------------------------------------------------------------------ hello, notification type, USB

def test_hello_stores_type_and_state(app, admin):
    with app.app_context():
        pk = _meter("Dryer A", "AAAA1111")
        d = db.session.get(Device, pk)
        ingest.handle(FakeConn(), d, {"t": "hello", "fw": "1.3.0", "fw_type": "pzem-ac", "hw": "esp32s3-n16r8", "proto": 1,
                                      "ota": {"state": "ok", "ver": "1.3.0"}, "caps": {"channels": 1, "ota": {"slot": 5, "layout": "ab"}}})
        db.session.commit()
        assert d.fw_type == "pzem-ac" and d.ota == {"state": "ok", "ver": "1.3.0", "hw": "esp32s3-n16r8"}
        assert d.caps["ota"]["layout"] == "ab"


def test_reboot_notification_type(app, admin):
    from app.services import devconfig
    assert "reboot_pending" in dict(devconfig.NOTIFY_TYPES) and "reboot_pending" in devconfig.NOTIFY_DEFAULT_ON


def test_usb_files(app, admin):
    upload(admin)
    with app.app_context():
        fid = Firmware.query.one().id
    man = admin.get(f"/firmware/{fid}/usb/manifest").get_json()
    assert man["chip"] == "esp32s3" and [p["offset"] for p in man["parts"]] == [0, 0x8000, 0xE000, 0x10000]
    assert man["flash"]["size"] == "16MB"
    for p in man["parts"]:
        r = admin.get(p["url"])
        assert r.status_code == 200 and len(r.data) == p["size"]
    assert admin.get(f"/firmware/{fid}/usb/file/passwd").status_code == 404
    assert admin.get(f"/firmware/{fid}/usb/file/..%2Fx").status_code == 404
    assert admin.post(f"/firmware/{fid}/usb/log", json={"ok": True, "erase": False, "mac": "AA:BB"}).get_json()["ok"]
    page = admin.get(f"/firmware/{fid}/flash").get_data(as_text=True)
    assert "By USB (browser)" in page and "Upload the Firmware to Meter" in page and "esptool-js/bundle.js" in page


def test_delete_keeps_job_history(app, admin, online):
    upload(admin)
    pk = ota_meter(app)
    with app.app_context():
        fid = Firmware.query.one().id
    admin.post(f"/firmware/{fid}/ota", json={"meter": pk, "confirm": True})
    with app.app_context():
        OtaJob.query.one().state = "ok"
        db.session.commit()
    assert admin.post("/firmware/delete", data={"ids": [str(fid)]}).status_code == 302
    with app.app_context():
        assert Firmware.query.count() == 0 and OtaJob.query.one().firmware_fk is None and OtaJob.query.one().fw_title
