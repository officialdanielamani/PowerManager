"""Hardware profile sent from the server (docs/protocol.md §12): rules, live / reboot, rollback, no archive."""
import copy

import pytest

from app.extensions import db
from app.models import Command, Device, MeterViewer, Notification, User
from app.services import commands, devconfig, hub, hwprofile, ingest

from .conftest import FakeConn
from .test_pairing import _code

CAPS = {"kind": "ac", "mc": True, "sensor": "rbamp", "variant": "UI2", "channels": 2, "relays": 2, "aux": 0,
        "voltage": True, "drdy": True, "amp_rating": 34.8, "ratings": [30, 30], "hw_cfg": 1}
DEFAULT = {"boards": [0x50], "relays": [{"id": 1, "drv": "xl9535", "addr": 0x20, "n": 2, "hi": 1}],
           "ch": [{"b": 1, "i": 1, "a": 30, "rb": 1, "ro": 1}, {"b": 1, "i": 2, "a": 30, "rb": 1, "ro": 2}]}
# the example of the design: 2 rbAmp boards, MCP23017 at 0x21 with 8 outputs, 7 channels
NEW = {"boards": [0x50, 0x51], "relays": [{"id": 1, "drv": "mcp23017", "addr": 0x21, "n": 8, "hi": 1}],
       "ch": [{"b": 1, "i": 1, "a": 10, "rb": 1, "ro": 1}, {"b": 1, "i": 2, "a": 10, "rb": 1, "ro": 2},
              {"b": 1, "i": 3, "a": 30, "rb": 1, "ro": 3}, {"b": 2, "i": 1, "a": 30, "rb": 1, "ro": 4},
              {"b": 2, "i": 2, "a": 30, "rb": 1, "ro": 5}, {"b": 2, "i": 3, "a": 100, "rb": 1, "ro": 6},
              {"b": 2, "i": 4, "a": 100, "rb": 1, "ro": 7}]}


@pytest.fixture()
def mc(app, admin):
    r = admin.post("/api/v1/pair", json={"code": _code(admin), "device_id": "HWP00001", "fw": "1.2.0", "proto": 1,
                                         "caps": CAPS})
    assert r.status_code == 200
    with app.app_context():
        d = Device.query.filter_by(device_id="HWP00001").one()
        hello(d, CAPS, "default", 0)
        db.session.commit()
        yield d.id


def hello(d, caps, state, ver, profile=None):
    ingest.handle(FakeConn(), d, {"t": "hello", "up": 10, "boot": 1, "cfg_ver": 0, "caps": caps,
                                  "hwp": {"ver": ver, "state": state, "profile": profile or DEFAULT}})


_seq = [100]


def evt(d, e, **data):
    _seq[0] += 1
    ingest.handle(FakeConn(), d, {"t": "evt", "seq": _seq[0], "ts": 1_800_000_000, "tv": 1, "e": e, **data})


def cmd_ack(d, cmd, ok=True, err=None, **extra):
    ingest.handle(FakeConn(), d, {"t": "cmd_ack", "ref": cmd.id, "ok": ok, **({"err": err} if err else {}), **extra})


def new_caps(profile):
    return dict(CAPS, channels=len(profile["ch"]), aux=hwprofile.aux_count(profile))


# ------------------------------------------------------------------ rules

def test_validate_rules():
    assert hwprofile.validate(DEFAULT) == [] and hwprofile.validate(NEW) == []

    def bad(mutate):
        p = copy.deepcopy(NEW)
        mutate(p)
        return hwprofile.validate(p)

    assert any("0x50 to 0x57" in e for e in bad(lambda p: p["boards"].__setitem__(1, 0x60)))
    assert any("used twice" in e for e in bad(lambda p: p["boards"].__setitem__(1, 0x50)))
    assert any("relay 3 is used twice" in e for e in bad(lambda p: p["ch"][3].update(rb=1, ro=3)))
    assert any("input 1 is used twice" in e for e in bad(lambda p: p["ch"][4].update(b=2, i=1)))
    assert any("outputs 1 to 8" in e for e in bad(lambda p: p["ch"][0].__setitem__("ro", 9)))
    assert any("no such relay board" in e for e in bad(lambda p: p["ch"][0].__setitem__("rb", 2)))
    assert any("choose a relay board" in e for e in bad(lambda p: p["ch"][0].__setitem__("rb", 0)))
    assert not bad(lambda p: p["ch"][0].update(rb=0, ro=0))  # a channel without relay is fine
    assert any("clamp model" in e for e in bad(lambda p: p["ch"][0].__setitem__("a", 15)))
    assert any("board must be" in e for e in bad(lambda p: p["ch"][0].__setitem__("b", 3)))
    assert any("input must be" in e for e in bad(lambda p: p["ch"][0].__setitem__("i", 8)))
    assert any("1 to 8 outputs" in e for e in bad(lambda p: p["relays"].append({"id": 2, "drv": "pcf8574", "addr": 0x38, "n": 9, "hi": 0})))
    assert any("address" in e for e in bad(lambda p: p["relays"][0].__setitem__("addr", 0x30)))
    assert any("At most 32" in e for e in bad(lambda p: p["relays"].extend(
        [{"id": 2, "drv": "xl9535", "addr": 0x22, "n": 16, "hi": 1}, {"id": 3, "drv": "xl9535", "addr": 0x23, "n": 16, "hi": 1}])))
    assert any("Channels" in e for e in bad(lambda p: p.__setitem__("ch", [])))
    assert any("at most 8 entries" in e for e in bad(lambda p: p["relays"].extend(
        [{"id": 2 + k, "drv": "pcf8574", "addr": 0x38 + k, "n": 1, "hi": 0} for k in range(7)] + [{"id": 1, "drv": "pcf8574", "addr": 0x3F, "n": 1, "hi": 0}])))
    assert hwprofile.validate({"boards": [0x50], "relays": [{"id": 1, "drv": "gpio", "addr": 0, "n": 3, "hi": 1}],
                               "ch": [{"b": 1, "i": 1, "a": 0, "rb": 1, "ro": 1}]}, {"gpio": 2})  # more GPIO relays than pins


def test_relay_boards_and_labels():
    two = copy.deepcopy(NEW)   # Board A = 2 outputs (XL9535), Board B = 8 (MCP23017): channels pick (board, output)
    two["relays"] = [{"id": 1, "drv": "xl9535", "addr": 0x20, "n": 2, "hi": 1}, {"id": 2, "drv": "mcp23017", "addr": 0x21, "n": 8, "hi": 1}]
    two["ch"] = [{"b": 1, "i": 1, "a": 10, "rb": 1, "ro": 2}, {"b": 2, "i": 1, "a": 30, "rb": 2, "ro": 8},
                 {"b": 2, "i": 2, "a": 30, "rb": 0, "ro": 0}]
    assert hwprofile.validate(two) == []
    assert hwprofile.relay_number(two, 1, 2) == 2 and hwprofile.relay_number(two, 2, 8) == 10 and hwprofile.relay_number(two, 0, 0) == 0
    assert hwprofile.relay_label(two, 2) == "Board A relay 2" and hwprofile.relay_label(two, 3) == "Board B relay 1"
    assert hwprofile.relay_label(two, 10) == "Board B relay 8" and hwprofile.relay_label({}, 4) == "relay 4"
    assert hwprofile.aux_count(two) == 8 and hwprofile.aux_numbers(two) == [1, 3, 4, 5, 6, 7, 8, 9]
    assert hwprofile.board_letter(1) == "A" and hwprofile.board_letter(2) == "B"


def test_relay_board_letters_are_stable():
    """Board A removed: Board B stays Board B (the profile keeps the ids, no renumbering)."""
    two = copy.deepcopy(DEFAULT)
    two["relays"] = [{"id": 1, "drv": "xl9535", "addr": 0x20, "n": 2, "hi": 1}, {"id": 2, "drv": "mcp23017", "addr": 0x21, "n": 8, "hi": 1}]
    two["ch"] = [{"b": 1, "i": 1, "a": 10, "rb": 2, "ro": 3}, {"b": 1, "i": 2, "a": 10, "rb": 2, "ro": 8}]
    assert hwprofile.validate(two) == []
    gone = copy.deepcopy(two)
    gone["relays"].pop(0)                     # Board A removed, channels still point at Board B
    assert hwprofile.validate(gone) == []
    assert hwprofile.relay_number(gone, 2, 3) == 3 and hwprofile.relay_number(two, 2, 3) == 5   # numbers across boards follow
    assert hwprofile.relay_label(gone, 3) == "Board B relay 3" and hwprofile.relay_label(gone, 8) == "Board B relay 8"
    assert hwprofile.aux_count(gone) == 6 and hwprofile.aux_numbers(gone) == [1, 2, 4, 5, 6, 7]
    assert hwprofile.next_relay_id(gone) == 1 and hwprofile.next_relay_id(two) == 3
    full = {"relays": [{"id": i} for i in range(1, 9)]}
    assert hwprofile.next_relay_id(full) is None
    # a channel on the removed Board A is refused, ids are unique and 1..8, normalisation sorts by id
    bad = copy.deepcopy(gone)
    bad["ch"][0].update(rb=1, ro=1)
    assert any("no such relay board" in e for e in hwprofile.validate(bad))
    dup = copy.deepcopy(two)
    dup["relays"][1]["id"] = 1
    assert any("used twice" in e for e in hwprofile.validate(dup))
    for badid in (0, 9, "x", None):
        e = copy.deepcopy(two)
        e["relays"][1]["id"] = badid
        assert any("id must be" in x for x in hwprofile.validate(e)), badid
    unsorted = copy.deepcopy(two)
    unsorted["relays"].reverse()
    assert [r["id"] for r in hwprofile.normalize(unsorted)["relays"]] == [1, 2]


def test_aux_count_and_mode():
    assert hwprofile.aux_count(NEW) == 1 and hwprofile.aux_count(DEFAULT) == 0
    clamps = copy.deepcopy(DEFAULT)
    clamps["ch"][0]["a"] = 10
    assert hwprofile.change_mode(DEFAULT, clamps) == "live"
    assert hwprofile.change_mode(DEFAULT, NEW) == "reboot"
    assert hwprofile.change_mode({}, clamps) == "reboot"  # nothing known about the meter yet
    moved = copy.deepcopy(DEFAULT)
    moved["relays"][0]["addr"] = 0x21
    assert hwprofile.change_mode(DEFAULT, moved) == "reboot"


# ------------------------------------------------------------------ flow

def test_hello_stores_profile_and_offers_aux_source(app, admin, mc):
    d = db.session.get(Device, mc)
    assert d.hw_state == "" and d.hw_current["ch"] == DEFAULT["ch"]
    assert "reboot" in devconfig.auxcfg.sources(d)


def test_reboot_flow_does_not_archive_the_meter(app, admin, mc):
    d = db.session.get(Device, mc)
    viewer = User(username="vee", role="viewer")
    viewer.set_password("Str0ng!Passw0rd")
    db.session.add(viewer)
    db.session.flush()
    db.session.add(MeterViewer(device_fk=d.id, user_fk=viewer.id, channels=[0, 1]))
    db.session.commit()

    cmd, errors = hwprofile.submit(d, NEW, delay_s=5)  # offline: queued; delay is clamped to 10 s
    assert not errors and cmd.params["mode"] == "reboot" and cmd.params["delay_s"] == 10
    assert cmd.params["ver"] == 1 and d.hw_ver == 1 and d.hw_state == "queued"
    cmd_ack(d, cmd, res={"state": "pending", "in_s": 10})
    assert d.hw_state == "pending" and d.hw_due_at

    # meter restarts with the trial profile and reports 7 channels (not "other hardware")
    hello(d, new_caps(NEW), "trial", 1, NEW)
    assert Device.query.filter(Device.device_id.like("HWP00001~%")).count() == 0
    assert d.hw_state == "trial" and d.is_mc and d.caps["channels"] == 7 and len(d.cfg["ch"]) == 7
    assert len(d.cfg["aux"]) == 1
    evt(d, "hw_active", ver=1)
    assert d.hw_state == "active" and d.hw_active_ver == 1 and d.hw_current["ch"] == NEW["ch"]
    assert any("The new hardware settings are active" in n.message for n in Notification.query.all())

    # the channel count is now 7: viewer links are not cut. Going down to 2 channels trims them.
    evt(d, "hw_active", ver=1)  # a repeated event does no harm
    hello(d, new_caps(NEW), "active", 1, NEW)
    assert d.hw_state == "active"


def test_viewer_channels_trimmed_when_channels_removed(app, admin, mc):
    d = db.session.get(Device, mc)
    viewer = User(username="vee", role="viewer")
    viewer.set_password("Str0ng!Passw0rd")
    db.session.add(viewer)
    db.session.flush()
    db.session.add(MeterViewer(device_fk=d.id, user_fk=viewer.id, channels=[0, 3, 6]))
    d.hw_current = dict(NEW, ver=1)
    d.hw_state, d.hw_active_ver, d.hw_ver = "active", 1, 1
    d.caps = new_caps(NEW)
    db.session.commit()
    smaller = copy.deepcopy(DEFAULT)
    cmd, errors = hwprofile.submit(d, smaller)
    assert not errors and d.hw_ver == 2
    hello(d, CAPS, "trial", 2, smaller)
    evt(d, "hw_active", ver=2)
    assert MeterViewer.query.filter_by(device_fk=d.id).one().channels == [0]
    assert d.hw_state == "active"


def test_live_clamp_change(app, admin, mc):
    d = db.session.get(Device, mc)
    p = copy.deepcopy(DEFAULT)
    p["ch"][0]["a"] = 100
    cmd, errors = hwprofile.submit(d, p, mode="live")
    assert not errors and cmd.params["mode"] == "live" and "delay_s" not in cmd.params
    cmd_ack(d, cmd, res={"state": "live"})
    assert d.hw_state == "active" and d.hw_current["ch"][0]["a"] == 100 and d.hw_active_ver == 1
    assert hwprofile.submit(d, NEW, mode="live")[1]  # more than clamps changed: refused
    assert hwprofile.submit(d, p)[1] == ["Nothing changed"]


def test_rollback(app, admin, mc):
    d = db.session.get(Device, mc)
    cmd, _ = hwprofile.submit(d, NEW)
    cmd_ack(d, cmd, res={"state": "pending", "in_s": 60})
    hello(d, new_caps(NEW), "trial", 1, NEW)
    evt(d, "hw_rollback", ver=1, reason="no_reply", addr=0x51)
    assert d.hw_state == "rolled_back" and d.hw_reason == "no_reply 0x51"
    n = [x.message for x in Notification.query.filter_by(type="meter_status")]
    assert any("rolled back: no_reply 0x51" in m for m in n)
    # the meter restarts with the previous profile: still "rolled back", not re-sent, not archived
    hello(d, CAPS, "default", 0, DEFAULT)
    assert d.hw_state == "rolled_back" and Device.query.filter(Device.device_id.like("HWP00001~%")).count() == 0
    assert d.is_mc and d.caps["channels"] == 2
    assert Command.query.filter_by(device_fk=d.id, c="hw_profile").count() == 1


def test_rejected_by_the_meter(app, admin, mc):
    d = db.session.get(Device, mc)
    cmd, _ = hwprofile.submit(d, NEW)
    cmd_ack(d, cmd, ok=False, err="invalid", detail="CH2: input 2 is used twice")
    assert d.hw_state == "rejected" and d.hw_reason == "invalid: CH2: input 2 is used twice"
    assert db.session.get(Command, cmd.id).ok is False


def test_newer_profile_replaces_queued_and_stale_ack_ignored(app, admin, mc):
    d = db.session.get(Device, mc)
    first, _ = hwprofile.submit(d, NEW)
    p2 = copy.deepcopy(NEW)
    p2["ch"][0]["a"] = 5
    second, _ = hwprofile.submit(d, p2)
    assert db.session.get(Command, first.id).cancelled and second.params["ver"] == 2
    cmd_ack(d, first, res={"state": "pending", "in_s": 60})  # answer to the old one
    assert d.hw_state == "queued"
    cancelled, _ = hwprofile.cancel(d)  # still queued: simply dropped
    assert cancelled is None and db.session.get(Command, second.id).cancelled and d.hw_state == ""


def test_factory_reset_is_not_overridden_by_the_old_profile(app, admin, mc):
    d = db.session.get(Device, mc)
    cmd, _ = hwprofile.submit(d, NEW)
    cmd_ack(d, cmd, res={"state": "pending", "in_s": 60})
    hello(d, new_caps(NEW), "trial", 1, NEW)
    evt(d, "hw_active", ver=1)
    assert d.hw_state == "active"
    hello(d, CAPS, "default", 0, DEFAULT)  # erased flash / new flash: the meter runs its config.h
    assert Command.query.filter_by(device_fk=d.id, c="hw_profile").count() == 1   # the old profile is NOT sent again
    assert d.hw_state == "" and not d.hw_profile and d.hw_current["ch"] == DEFAULT["ch"]


def test_live_clamp_change_updates_the_ratings_shown(app, admin, mc):
    """The Configuration page shows caps.ratings: a live clamp change must update them (and the meter's next hello confirms)."""
    d = db.session.get(Device, mc)
    hello(d, new_caps(NEW), "active", 1, NEW)
    cmd, _ = hwprofile.submit(d, dict(NEW, ch=[dict(c, a=20) for c in NEW["ch"]]))
    cmd_ack(d, cmd, res={"state": "live"})
    db.session.commit()
    assert d.caps["ratings"] == [20] * len(NEW["ch"])


def test_flashed_config_h_replaces_the_stored_profile(app, admin, mc):
    """config.h changed (e.g. clamp 30 A -> 20 A) and flashed without erasing: the meter drops the stored profile
    (hello.hwp.reset = "config") and the server forgets its wanted profile instead of sending it back."""
    d = db.session.get(Device, mc)
    cmd, _ = hwprofile.submit(d, NEW)
    cmd_ack(d, cmd, res={"state": "pending", "in_s": 60})
    hello(d, new_caps(NEW), "trial", 1, NEW)
    evt(d, "hw_active", ver=1)
    assert d.hw_state == "active"
    before = Command.query.filter_by(device_fk=d.id, c="hw_profile").count()
    changed = dict(DEFAULT, ch=[dict(DEFAULT["ch"][0], a=20)] + list(DEFAULT["ch"][1:]))
    ingest.handle(FakeConn(), d, {"t": "hello", "up": 10, "boot": 2, "cfg_ver": 0, "caps": CAPS,
                                  "hwp": {"ver": 0, "state": "default", "reset": "config", "profile": changed}})
    db.session.commit()
    assert Command.query.filter_by(device_fk=d.id, c="hw_profile").count() == before   # nothing sent again
    assert d.hw_profile == {} and d.hw_ver == 0 and d.hw_state == "" and d.hw_current["ch"][0]["a"] == 20


def test_aux_conflict_blocks_save(app, admin, mc):
    d = db.session.get(Device, mc)
    d.caps = new_caps(NEW)
    devconfig.fit_channels(d)
    d.hw_current = dict(NEW, ver=1)
    d.hw_state, d.hw_ver, d.hw_active_ver = "active", 1, 1
    cfg = copy.deepcopy(d.cfg)
    cfg["aux"][0].update(name="Pump", mode="schedule")
    d.cfg = cfg
    db.session.commit()
    cmd, errors = hwprofile.submit(d, DEFAULT)  # no AUX relay left
    assert cmd is None and errors and "AUX1" in errors[0]


def test_old_firmware_and_invalid_profile_refused(app, admin, mc):
    d = db.session.get(Device, mc)
    bad = copy.deepcopy(NEW)
    bad["boards"] = [0x50, 0x50]
    assert hwprofile.submit(d, bad)[1] and d.hw_ver == 0
    d.caps = {k: v for k, v in CAPS.items() if k != "hw_cfg"}
    assert "does not support" in hwprofile.submit(d, NEW)[1][0]


def test_scan_and_address_need_an_online_meter(app, admin, mc, monkeypatch):
    d = db.session.get(Device, mc)
    assert hwprofile.scan(d)[1] == "offline"
    assert hwprofile.set_rbamp_address(d, 0x50, 0x60)[1]
    assert hwprofile.set_rbamp_address(d, 0x50, 0x50)[1]
    monkeypatch.setattr(hub, "is_online", lambda pk: True)
    sent = []
    monkeypatch.setattr(hub, "send", lambda pk, msg: sent.append(msg) or 1)
    cmd, err = hwprofile.scan(d)
    assert err is None and sent[0]["c"] == "i2c_scan"
    res = {"found": [0x20, 0x50], "rbamp": [0x50]}
    cmd_ack(d, cmd, res=res)
    assert db.session.get(Command, cmd.id).res == res
    cmd, err = hwprofile.set_rbamp_address(d, 0x50, 0x51)
    assert err == [] and sent[1]["from"] == 0x50 and sent[1]["to"] == 0x51
    cmd_ack(d, cmd, ok=False, err="busy")
    assert db.session.get(Command, cmd.id).err == "busy"


def test_old_hello_without_hwp_is_unchanged(app, admin):
    r = admin.post("/api/v1/pair", json={"code": _code(admin), "device_id": "HWP00002", "fw": "1.0.0", "proto": 1,
                                         "caps": {k: v for k, v in CAPS.items() if k != "hw_cfg"}})
    assert r.status_code == 200
    with app.app_context():
        d = Device.query.filter_by(device_id="HWP00002").one()
        ingest.handle(FakeConn(), d, {"t": "hello", "up": 1, "boot": 1, "cfg_ver": 0,
                                      "caps": {k: v for k, v in CAPS.items() if k != "hw_cfg"}})
        assert d.hw_state == "" and "reboot" not in devconfig.auxcfg.sources(d)
        assert commands.KNOWN >= {"hw_profile", "hw_cancel", "i2c_scan", "rbamp_addr"}
