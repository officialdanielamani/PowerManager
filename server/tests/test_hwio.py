"""Update Device I/O pages (Firmware): access, scan, apply, cancel, address change, generator start-from-meter."""
import copy

import pytest

from app.extensions import db
from app.models import Command, Device, User
from app.services import hub, hwprofile, ingest

from .conftest import FakeConn
from .test_access import VPW, _login, _meter
from .test_hwprofile import CAPS, DEFAULT, NEW, hello
from .test_manager import MPW, _account
from .test_pairing import _code


@pytest.fixture()
def mc(app, admin):
    r = admin.post("/api/v1/pair", json={"code": _code(admin), "device_id": "HWIO0001", "fw": "1.1.0", "proto": 1, "caps": CAPS})
    assert r.status_code == 200
    with app.app_context():  # (not kept open around the requests: flask-login would reuse the cached user)
        d = Device.query.filter_by(device_id="HWIO0001").one()
        hello(d, CAPS, "default", 0)
        db.session.commit()
        return d.id


@pytest.fixture()
def online(monkeypatch):
    sent = []
    monkeypatch.setattr(hub, "is_online", lambda pk: True)
    monkeypatch.setattr(hub, "send", lambda pk, msg: sent.append(msg) or 1)
    return sent


def test_firmware_page_has_the_button(app, admin):
    html = admin.get("/firmware").get_data(as_text=True)
    assert "Update Device I/O" in html and "Generate config.h" in html


def test_page_lists_meters_and_renders_the_editor(app, admin, mc):
    html = admin.get("/firmware/io").get_data(as_text=True)
    assert "Select meter" in html and "HWIO0001" in html and 'id="io"' not in html
    html = admin.get(f"/firmware/io?meter={mc}").get_data(as_text=True)
    assert 'id="io"' in html and "Scan Address" in html and "Update Clamp Model" in html and "Save &amp; Apply" in html
    assert "SCT-013-100 (100 A)" in html and "MCP23017" in html


def test_old_firmware_gets_a_hint(app, admin):
    old = {k: v for k, v in CAPS.items() if k != "hw_cfg"}
    assert admin.post("/api/v1/pair", json={"code": _code(admin), "device_id": "HWIO0002", "fw": "1.0.0", "proto": 1,
                                            "caps": old}).status_code == 200
    with app.app_context():
        pk = Device.query.filter_by(device_id="HWIO0002").one().id
    assert "does not support hardware profiles" in admin.get(f"/firmware/io?meter={pk}").get_data(as_text=True)


def test_status_apply_cancel(app, admin, mc, online):
    s = admin.get(f"/firmware/io/{mc}/status").get_json()
    assert s["ok"] and s["hw_cfg"] and s["state"] == "" and s["current"]["ch"] == DEFAULT["ch"] and s["online"]
    bad = admin.post(f"/firmware/io/{mc}/apply", json={"mode": "reboot", "profile": {"boards": [0x60]}})
    assert bad.status_code == 400 and bad.get_json()["errors"]
    assert admin.post(f"/firmware/io/{mc}/apply", json={"mode": "x", "profile": NEW}).status_code == 400
    # more than clamps changed: "live" is refused
    assert admin.post(f"/firmware/io/{mc}/apply", json={"mode": "live", "profile": NEW}).status_code == 400
    ok = admin.post(f"/firmware/io/{mc}/apply", json={"mode": "reboot", "profile": NEW, "delay_s": 30}).get_json()
    assert ok["ok"] and ok["mode"] == "reboot" and ok["ver"] == 1 and not ok["queued"]
    assert online[-1]["c"] == "hw_profile" and online[-1]["delay_s"] == 30 and online[-1]["profile"]["ch"] == NEW["ch"]
    assert admin.get(f"/firmware/io/{mc}/status").get_json()["state"] == "queued"
    with app.app_context():
        d = db.session.get(Device, mc)
        cmd = Command.query.filter_by(c="hw_profile").one()
        ingest.handle(FakeConn(), d, {"t": "cmd_ack", "ref": cmd.id, "ok": True, "res": {"state": "pending", "in_s": 30}})
        db.session.commit()
    s = admin.get(f"/firmware/io/{mc}/status").get_json()
    assert s["state"] == "pending" and 0 < s["due_in"] <= 30
    assert admin.post(f"/firmware/io/{mc}/cancel").get_json()["ok"]
    assert online[-1]["c"] == "hw_cancel"


def test_live_clamp_update_from_the_page(app, admin, mc, online):
    p = copy.deepcopy(DEFAULT)
    p["ch"][1]["a"] = 100
    r = admin.post(f"/firmware/io/{mc}/apply", json={"mode": "live", "profile": p}).get_json()
    assert r["ok"] and r["mode"] == "live" and "delay_s" not in online[-1]


def test_scan_and_address_wizard(app, admin, mc, online):
    r = admin.post(f"/firmware/io/{mc}/scan").get_json()
    assert r["ok"] and online[-1]["c"] == "i2c_scan"
    with app.app_context():
        d = db.session.get(Device, mc)
        cmd = db.session.get(Command, r["cmd"])
        ingest.handle(FakeConn(), d, {"t": "cmd_ack", "ref": cmd.id, "ok": True, "res": {"found": [32, 80], "rbamp": [80]}})
        db.session.commit()
    c = admin.get(f"/firmware/io/{mc}/cmd/{r['cmd']}").get_json()
    assert c["done"] and c["success"] and c["res"]["rbamp"] == [80]
    assert admin.post(f"/firmware/io/{mc}/addr", json={"from": 80, "to": 81}).get_json()["ok"]
    assert online[-1]["c"] == "rbamp_addr" and online[-1]["to"] == 81
    bad = admin.post(f"/firmware/io/{mc}/addr", json={"from": 80, "to": 80})
    assert bad.status_code == 409 and "same" in bad.get_json()["error"]
    assert admin.post(f"/firmware/io/{mc}/addr", json={"from": "x"}).status_code == 400
    assert admin.get(f"/firmware/io/{mc}/cmd/999").status_code == 404


def test_scan_needs_an_online_meter(app, admin, mc):
    r = admin.post(f"/firmware/io/{mc}/scan")
    assert r.status_code == 409 and "offline" in r.get_json()["error"]


def test_access_rules(app, admin, mc):
    with app.app_context():
        plain = _meter("Single", "SING0001")  # not multi-channel
    assert admin.get(f"/firmware/io/{plain}/status").status_code == 404
    # a viewer cannot use any of it, a manager only for its own meters
    _account(app, admin, "tenant", "viewer", [mc])
    v = _login(app, "tenant")
    assert v.get("/firmware/io").status_code == 403
    assert v.get(f"/firmware/io/{mc}/status").status_code == 403
    assert v.post(f"/firmware/io/{mc}/scan").status_code == 403
    adam = _account(app, admin, "adam", "manager")
    m = _login(app, "adam", MPW)
    assert m.get("/firmware/io").status_code == 200 and "HWIO0001" not in m.get("/firmware/io").get_data(as_text=True)
    assert m.get(f"/firmware/io/{mc}/status").status_code == 404   # not visible to adam
    assert m.post(f"/firmware/io/{mc}/apply", json={"mode": "reboot", "profile": NEW}).status_code == 404
    with app.app_context():
        db.session.get(Device, mc).owner_fk = adam
        db.session.commit()
    assert m.get(f"/firmware/io/{mc}/status").status_code == 200
    assert "HWIO0001" in m.get("/firmware/io").get_data(as_text=True)


def test_generator_starts_from_the_meter(app, admin, mc):
    assert admin.get(f"/firmware/config/from-meter/{mc}").get_json()["ok"]
    with app.app_context():
        d = db.session.get(Device, mc)
        d.hw_current = dict(NEW, ver=3)
        db.session.commit()
    j = admin.get(f"/firmware/config/from-meter/{mc}").get_json()
    assert j["boards"] == ["0x50", "0x51"]
    assert j["relays"] == [{"driver": "RELAY_MCP23017", "addr": "0x21", "n": 8, "high": True}]
    assert j["channels"][0] == {"board": 1, "input": 1, "clamp": "SCT_10A", "relay_board": 1, "relay_out": 1}
    assert j["channels"][5]["clamp"] == "SCT_100A" and len(j["channels"]) == 7
    html = admin.get("/firmware/config").get_data(as_text=True)
    assert 'id="gFrom"' in html and "Start from the hardware of a meter" in html
    # the generated config.h really takes those values
    from app.services import cfggen
    form = {"meter": "rbamp", "pin_sda": 8, "pin_scl": 9, "pin_led": 48, "pin_drdy": 5, "drdy": True, "tamper": True, "pin_tamper": 4,
            "net_ws": True, "net_mqtt": True, "mesh": True, "ap_pass_id": True, **{k: j[k] for k in ("boards", "relays", "channels")}}
    text, errors, _ = cfggen.generate(form)
    assert not errors and "{ 0x50, 0x51 }" in text and "{RELAY_MCP23017, 0x21, 8, 1}" in text and "SCT_100A" in text


def test_no_profile_known_yet(app, admin):
    assert admin.post("/api/v1/pair", json={"code": _code(admin), "device_id": "HWIO0003", "fw": "1.1.0", "proto": 1,
                                            "caps": CAPS}).status_code == 200
    with app.app_context():
        pk = Device.query.filter_by(device_id="HWIO0003").one().id
    r = admin.get(f"/firmware/config/from-meter/{pk}")
    assert r.status_code == 404 and not r.get_json()["ok"]
    assert 'id="gFrom"' not in admin.get("/firmware/config").get_data(as_text=True)


def test_generator_relay_boards_a_and_b(app, admin):
    """Channels pick a relay board (A, B ...) and an output on it; config.h gets the 5-column CHANNELS table."""
    from app.services import cfggen
    form = {"meter": "rbamp", "pin_sda": 8, "pin_scl": 9, "pin_led": 48, "pin_drdy": 5, "drdy": True, "tamper": True, "pin_tamper": 4,
            "net_ws": True, "net_mqtt": True, "mesh": True, "ap_pass_id": True, "boards": ["0x50", "0x51"],
            "relays": [{"driver": "RELAY_XL9535", "addr": "0x20", "n": 2, "high": True},
                       {"driver": "RELAY_MCP23017", "addr": "0x21", "n": 8, "high": True}],
            "channels": [{"board": 1, "input": 1, "clamp": "SCT_100A", "relay_board": 1, "relay_out": 2},
                         {"board": 2, "input": 1, "clamp": "SCT_30A", "relay_board": 2, "relay_out": 8},
                         {"board": 2, "input": 2, "clamp": "SCT_30A", "relay_board": 0, "relay_out": 0}]}
    text, errors, notes = cfggen.generate(form)
    assert not errors
    assert "{  1,     1,     SCT_100A, 1,           2 },   /* CH1 */" in text
    assert "{  2,     1,     SCT_30A,  2,           8 },   /* CH2 */" in text and "{  2,     2,     SCT_30A,  0,           0 }" in text
    assert any("relay 2 = CH1" in n and "relay 10 = CH2" in n for n in notes)
    for bad, msg in (({"relay_board": 3, "relay_out": 1}, "relay board"), ({"relay_board": 1, "relay_out": 3}, "relay output"),
                     ({"relay_board": 1, "relay_out": 0}, "choose the output"), ({"relay_board": 1, "relay_out": 2}, "already used")):
        f = copy.deepcopy(form)
        f["channels"][2].update(bad)
        assert any(msg in e for e in cfggen.generate(f)[1]), (bad, msg)
    # the generated file still has the shape the firmware's CHANNELS table expects
    import re
    assert re.search(r"#define CHANNELS \{ \\\n  /\* board  input  clamp     relay board  output \*/ \\\n", text)


def test_old_firmware_is_outdated(app):
    from app.services import devconfig
    assert devconfig.MIN_FW == (1, 1, 0)

    class D:
        fw = "1.0.0"
    assert devconfig.fw_outdated(D)
    D.fw = "1.1.0"
    assert not devconfig.fw_outdated(D)
