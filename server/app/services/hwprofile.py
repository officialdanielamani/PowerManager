"""Hardware profile of an rbAmp meter: which rbAmp modules, relay boards and channel wiring it uses.

The server keeps the wanted profile (Device.hw_profile) and sends it with the `hw_profile` command; the meter
validates it, restarts after a delay, checks that every part answers and rolls back if not
(docs/protocol.md §12). This module holds the rules (the same ones the firmware applies), the change
classification (live clamp change / reboot), and the handling of the meter's answers and events.
"""
from ..extensions import db
from ..models import Command, MeterViewer, now
from . import auxcfg, commands, devconfig, notify

PROFILE_FORMAT = 1
CLAMPS = (0, 5, 10, 20, 30, 50, 60, 100)   # amps, 0 = keep what the module has
CLAMP_LABELS = {0: "Keep (CT module)", 5: "SCT-013-005 (5 A)", 10: "SCT-013-010 (10 A)", 20: "SCT-013-020 (20 A)",
                30: "SCT-013-030 (30 A)", 50: "SCT-013-050 (50 A)", 60: "SCT-013-060 (60 A)",
                100: "SCT-013-100 (100 A)"}
DRIVERS = {"mcp23017": 16, "xl9535": 16, "pcf8574": 8, "gpio": 16}   # driver -> most outputs
DRIVER_LABELS = {"mcp23017": "MCP23017", "xl9535": "XL9535 / TCA9535", "pcf8574": "PCF8574 / PCF8574A",
                 "gpio": "ESP32 pins"}
RBAMP_ADDR = range(0x50, 0x58)
MAX_BOARDS, MAX_CHANNELS, MAX_RELAYS, MAX_RELAY_BOARDS = 8, 16, 32, 8
DELAY_DEFAULT, DELAY_MIN, DELAY_MAX = 60, 10, 600
STATES = ("", "queued", "pending", "trial", "active", "rolled_back", "rejected")


def _int(x):
    return x if isinstance(x, int) and not isinstance(x, bool) else None


def _addr_ok(drv, addr):
    if drv == "gpio":
        return addr == 0
    if drv == "pcf8574":
        return 0x20 <= addr <= 0x27 or 0x38 <= addr <= 0x3F
    return 0x20 <= addr <= 0x27


def relay_total(profile):
    return sum(r.get("n", 0) for r in profile.get("relays") or [])


def aux_count(profile):
    """Relays that no channel uses: AUX relays (same rule as the firmware)."""
    used = {(c.get("rb"), c.get("ro")) for c in profile.get("ch") or [] if c.get("rb")}
    return relay_total(profile) - len(used)


def board_letter(rb):
    """Relay board id 1, 2, 3 ... is shown as Board A, B, C ..."""
    return chr(64 + rb)


def _by_id(profile):
    return sorted(profile.get("relays") or [], key=lambda r: r.get("id", 0))


def relay_number(profile, rb, ro):
    """(relay board id, output) -> relay number across all boards (board with the lowest id first; the number AUX
    relays and the meter use); 0 = none."""
    if not rb:
        return 0
    n = 0
    for r in _by_id(profile):
        if r.get("id") == rb:
            return n + ro
        n += r.get("n", 0)
    return 0


def relay_label(profile, number):
    """Relay number across all boards -> "Board B relay 3" (falls back to "relay N")."""
    left = number
    for r in _by_id(profile):
        if left <= r.get("n", 0):
            return f"Board {board_letter(r.get('id', 0))} relay {left}"
        left -= r.get("n", 0)
    return f"relay {number}"


def next_relay_id(profile):
    """Lowest free relay board id (a new board gets the first free letter), None when all 8 are used."""
    used = {r.get("id") for r in profile.get("relays") or []}
    return next((i for i in range(1, MAX_RELAY_BOARDS + 1) if i not in used), None)


def aux_numbers(profile):
    """Relay numbers (across all boards) of the AUX relays, in order."""
    used = {relay_number(profile, c.get("rb"), c.get("ro")) for c in profile.get("ch") or [] if c.get("rb")}
    return [r for r in range(1, relay_total(profile) + 1) if r not in used]


def validate(profile, limits=None):
    """Rules of the firmware's static_asserts (hwmap.h / relaymap.h). Returns a list of error texts (empty = ok).

    `limits` = caps.hw_limits reported by the meter, e.g. {"gpio": 2}: relay pins its build offers."""
    errors = []
    if not isinstance(profile, dict):
        return ["Profile must be an object"]
    boards, relays, chans = profile.get("boards"), profile.get("relays"), profile.get("ch")
    if not isinstance(boards, list) or not 1 <= len(boards) <= MAX_BOARDS:
        errors.append(f"rbAmp boards: 1 to {MAX_BOARDS} addresses")
        boards = []
    seen = set()
    for i, a in enumerate(boards, 1):
        if _int(a) is None or a not in RBAMP_ADDR:
            errors.append(f"rbAmp board {i}: address must be 0x50 to 0x57")
        elif a in seen:
            errors.append(f"rbAmp board {i}: address 0x{a:02X} is used twice")
        else:
            seen.add(a)
    if not isinstance(relays, list):
        errors.append("Relay boards: a list is required")
        relays = []
    elif len(relays) > MAX_RELAY_BOARDS:
        errors.append(f"Relay boards: at most {MAX_RELAY_BOARDS} entries")
        relays = relays[:MAX_RELAY_BOARDS]
    total, gpio, seen_r = 0, 0, set()
    sizes = {}  # relay board id -> outputs (0 for an invalid entry), for the channel check
    for i, r in enumerate(relays, 1):
        if not isinstance(r, dict) or r.get("drv") not in DRIVERS:
            errors.append(f"Relay board {i}: driver must be one of {', '.join(DRIVERS)}")
            continue
        rid = _int(r.get("id"))
        if rid is None or not 1 <= rid <= MAX_RELAY_BOARDS:
            errors.append(f"Relay board {i}: id must be 1 to {MAX_RELAY_BOARDS} (Board A to H)")
            continue
        if rid in sizes:
            errors.append(f"Relay board {i}: Board {board_letter(rid)} is used twice")
            continue
        sizes[rid] = 0
        drv, addr, n = r["drv"], _int(r.get("addr")), _int(r.get("n"))
        if addr is None or not _addr_ok(drv, addr):
            errors.append(f"Relay board {i}: invalid I2C address for {DRIVER_LABELS[drv]}")
        elif drv != "gpio":
            if addr in seen_r or addr in seen:
                errors.append(f"Relay board {i}: address 0x{addr:02X} is used twice")
            seen_r.add(addr)
        if n is None or not 1 <= n <= DRIVERS[drv]:
            errors.append(f"Relay board {i}: {DRIVER_LABELS[drv]} has 1 to {DRIVERS[drv]} outputs")
            continue
        sizes[rid] = n
        if r.get("hi") not in (0, 1, True, False):
            errors.append(f"Relay board {i}: active level must be high or low")
        total += n
        if drv == "gpio":
            gpio += n
    if total > MAX_RELAYS:
        errors.append(f"At most {MAX_RELAYS} relays in total ({total} listed)")
    if limits and isinstance(limits.get("gpio"), int) and gpio > limits["gpio"]:
        errors.append(f"This meter's firmware offers {limits['gpio']} GPIO relay pin(s), {gpio} requested")
    if not isinstance(chans, list) or not 1 <= len(chans) <= MAX_CHANNELS:
        errors.append(f"Channels: 1 to {MAX_CHANNELS}")
        chans = []
    seen_in, seen_rl = set(), set()
    for i, c in enumerate(chans, 1):
        if not isinstance(c, dict):
            errors.append(f"CH{i}: invalid entry")
            continue
        b, inp, a, rb, ro = (_int(c.get(k)) for k in ("b", "i", "a", "rb", "ro"))
        if b is None or not 1 <= b <= max(len(boards), 1) or (boards and b > len(boards)):
            errors.append(f"CH{i}: board must be 1 to {len(boards)}")
        if inp is None or not 1 <= inp <= 7:
            errors.append(f"CH{i}: input must be 1 to 7")
        if a not in CLAMPS:
            errors.append(f"CH{i}: clamp model must be one of {', '.join(str(x) for x in CLAMPS if x)} A or keep")
        if rb is None or ro is None or (rb != 0 and rb not in sizes):
            errors.append(f"CH{i}: no such relay board" if rb else f"CH{i}: invalid relay")
        elif rb == 0 and ro != 0:
            errors.append(f"CH{i}: choose a relay board for output {ro}, or leave both empty")
        elif rb and not 1 <= ro <= sizes[rb]:
            errors.append(f"CH{i}: Board {board_letter(rb)} has outputs 1 to {sizes[rb]}")
        if b is not None and inp is not None:
            if (b, inp) in seen_in:
                errors.append(f"CH{i}: board {b} input {inp} is used twice")
            seen_in.add((b, inp))
        if rb:
            if (rb, ro) in seen_rl:
                errors.append(f"CH{i}: Board {board_letter(rb)} relay {ro} is used twice")
            seen_rl.add((rb, ro))
    return errors


def normalize(profile):
    """Canonical form (ints, only known keys) of a valid profile."""
    return {
        "boards": [int(a) for a in profile["boards"]],
        "relays": sorted(({"id": int(r["id"]), "drv": r["drv"], "addr": int(r["addr"]), "n": int(r["n"]),
                           "hi": 1 if r.get("hi") else 0} for r in profile["relays"]), key=lambda r: r["id"]),
        "ch": [{"b": int(c["b"]), "i": int(c["i"]), "a": int(c.get("a") or 0), "rb": int(c.get("rb") or 0),
                "ro": int(c.get("ro") or 0)} for c in profile["ch"]],
    }


def _structure(p):
    """Everything except the clamp models."""
    return (p.get("boards"), p.get("relays"),
            [(c.get("b"), c.get("i"), c.get("rb"), c.get("ro")) for c in p.get("ch") or []])


def change_mode(current, wanted):
    """"live" when only clamp models differ (applied at once, no reboot), else "reboot"."""
    if current and current.get("ch") and _structure(current) == _structure(wanted):
        return "live"
    return "reboot"


def aux_conflicts(device, profile):
    """AUX relays that the new profile removes but that still have an automation or a name."""
    have = (device.cfg or {}).get("aux") or []
    keep = aux_count(profile)
    return [f"AUX{j + 1} is configured but the new profile has only {keep} AUX relay(s): clear it first"
            for j in range(keep, len(have)) if have[j] != auxcfg.default()]


def clamp_summary(profile):
    return [{"ch": i, **c} for i, c in enumerate(profile.get("ch") or [])]


# ---------------------------------------------------------------- server -> meter

def next_ver(device):
    return max(device.hw_ver or 0, device.hw_active_ver or 0, int((device.hw_current or {}).get("ver") or 0)) + 1


def submit(device, profile, user=None, mode=None, delay_s=DELAY_DEFAULT):
    """Validate a wanted profile and send it. mode: None = decide, "live" = clamp models only (refused when more
    changed), "reboot" = always restart. Returns (command, errors)."""
    if not (device.caps or {}).get("hw_cfg"):
        return None, ["This meter's firmware does not support hardware profiles"]
    errors = validate(profile, (device.caps or {}).get("hw_limits"))
    if errors:
        return None, errors
    prof = normalize(profile)
    errors = aux_conflicts(device, prof)
    if errors:
        return None, errors
    cur = (device.hw_current or {})
    decided = "reboot" if mode == "reboot" else change_mode(cur, prof)
    if mode == "live" and decided != "live":
        return None, ["More than the clamp models changed: use Save & Apply (reboot)"]
    if cur and {k: cur.get(k) for k in ("boards", "relays", "ch")} == prof and device.hw_state in ("active", ""):
        return None, ["Nothing changed"]
    try:
        delay = max(DELAY_MIN, min(DELAY_MAX, int(delay_s)))
    except (TypeError, ValueError):
        delay = DELAY_DEFAULT
    ver = next_ver(device)
    device.hw_profile = prof
    device.hw_ver = ver
    device.hw_state, device.hw_reason, device.hw_due_at = "queued", None, None
    params = {"ver": ver, "profile": prof, "mode": decided}
    if decided == "reboot":
        params["delay_s"] = delay
    cmd, err = commands.issue(device, "hw_profile", params, user)
    if err:
        return None, [err]
    return cmd, []


def cancel(device, user=None):
    """Cancel a reboot countdown (or a profile still waiting to be sent)."""
    for old in Command.query.filter_by(device_fk=device.id, c="hw_profile", sent_at=None, cancelled=False).all():
        old.cancelled = True
    if device.hw_state == "queued":
        device.hw_state = "active" if device.hw_active_ver else ""
        db.session.commit()
        return None, None
    return commands.issue(device, "hw_cancel", {}, user)


def scan(device, user=None):
    return commands.issue(device, "i2c_scan", {}, user)


def set_rbamp_address(device, old, new, user=None):
    errors = []
    if old not in RBAMP_ADDR or new not in RBAMP_ADDR:
        errors.append("rbAmp addresses are 0x50 to 0x57")
    elif old == new:
        errors.append("The new address is the same as the old one")
    if errors:
        return None, errors
    cmd, err = commands.issue(device, "rbamp_addr", {"from": old, "to": new}, user)
    return cmd, ([err] if err else [])


# ---------------------------------------------------------------- meter -> server

def _trim_viewers(device):
    n = devconfig.channel_count(device)
    for link in MeterViewer.query.filter_by(device_fk=device.id).all():
        if isinstance(link.channels, list):
            link.channels = [i for i in link.channels if i < n]


def on_hello(device, hwp, conn_send=None):
    """hello.hwp = {ver, state, profile}: what the meter uses now. Returns True if the wanted profile is re-sent."""
    if not isinstance(hwp, dict):
        return False
    state = hwp.get("state")
    ver = hwp.get("ver") if isinstance(hwp.get("ver"), int) else 0
    if isinstance(hwp.get("profile"), dict):
        device.hw_current = dict(hwp["profile"], ver=ver)
    if state in ("trial", "pending"):
        device.hw_state = state
    elif state == "active":
        device.hw_active_ver = ver
        if device.hw_state == "rolled_back" and ver != device.hw_ver:
            pass  # rolled back: the meter reports the previous profile
        else:
            device.hw_state = "active"
            device.hw_reason = None
    elif state == "default":
        device.hw_active_ver = 0
        if (device.hw_ver or 0) > 0 and device.hw_profile and device.hw_state not in (
                "rolled_back", "pending", "trial", "queued", "rejected"):
            # Factory reset or a new flash: the server still has the profile, send it again.
            cmd, err = commands.issue(device, "hw_profile",
                                      {"ver": device.hw_ver, "profile": device.hw_profile, "mode": "reboot",
                                       "delay_s": DELAY_DEFAULT})
            if cmd is not None:
                device.hw_state, device.hw_due_at = "queued", None
                return True
        elif device.hw_state == "active":
            device.hw_state = ""
    return False


def profile_change_expected(device, hwp, new_sig_kind_same):
    """True when a changed channel count in hello comes from a profile change (not from other hardware)."""
    if not new_sig_kind_same or not isinstance(hwp, dict):
        return False
    if hwp.get("state") in ("trial", "active", "pending"):
        return True
    return hwp.get("state") == "default" and device.hw_state in ("rolled_back", "trial", "pending")


def on_ack(device, cmd, ok, err, res):
    """cmd_ack of hw_profile / hw_cancel."""
    res = res if isinstance(res, dict) else {}
    if cmd.c == "hw_profile":
        if (cmd.params or {}).get("ver") != device.hw_ver:
            return  # an older profile: ignore
        if ok:
            if res.get("state") == "live":
                device.hw_current = dict(device.hw_profile, ver=device.hw_ver)
                device.hw_state, device.hw_active_ver, device.hw_reason, device.hw_due_at = (
                    "active", device.hw_ver, None, None)
            else:
                device.hw_state, device.hw_reason = "pending", None
                try:
                    device.hw_due_at = now() + int(res.get("in_s", DELAY_DEFAULT))
                except (TypeError, ValueError):
                    device.hw_due_at = now() + DELAY_DEFAULT
        else:
            device.hw_state = "rejected"
            device.hw_reason = (str(err or "rejected") + (f": {res['detail']}" if res.get("detail") else ""))[:100]
            device.hw_due_at = None
    elif cmd.c == "hw_cancel" and ok:
        device.hw_state = "active" if device.hw_active_ver else ""
        device.hw_due_at = None


def on_event(device, ev):
    """hw_reboot_pending / hw_active / hw_rollback from the meter."""
    d = ev.data or {}
    ver = d.get("ver") if isinstance(d.get("ver"), int) else None
    if ev.type == "hw_reboot_pending":
        device.hw_state = "pending"
        try:
            device.hw_due_at = ev.ts + int(d.get("in_s", DELAY_DEFAULT))
        except (TypeError, ValueError):
            device.hw_due_at = None
        notify.notify(device, "reboot_pending",
                      f"Meter restarts in {int(d.get('in_s') or DELAY_DEFAULT)} s: hardware change (profile v{ver if ver is not None else '?'})")
    elif ev.type == "hw_active":
        device.hw_active_ver = ver or device.hw_active_ver
        device.hw_due_at = None
        if ver == device.hw_ver:
            device.hw_current = dict(device.hw_profile, ver=ver)
            device.hw_state, device.hw_reason = "active", None
            _trim_viewers(device)
            notify.notify(device, "meter_status", f"Hardware profile v{ver} is active")
    elif ev.type == "hw_rollback":
        reason = str(d.get("reason") or "unknown")
        if d.get("addr") is not None:
            try:
                reason += f" 0x{int(d['addr']):02X}"
            except (TypeError, ValueError):
                pass
        device.hw_state, device.hw_reason, device.hw_due_at = "rolled_back", reason[:100], None
        notify.notify(device, "meter_status",
                      f"Hardware profile v{ver if ver is not None else '?'} rolled back: {reason}. "
                      "The meter uses its previous profile")


def view(device):
    """State for the Update Device I/O page."""
    caps = device.caps or {}
    due = None
    if device.hw_state == "pending" and device.hw_due_at:
        due = max(0, device.hw_due_at - now())
    return {
        "hw_cfg": bool(caps.get("hw_cfg")), "fw": device.fw, "state": device.hw_state or "",
        "reason": device.hw_reason, "ver": device.hw_ver, "active_ver": device.hw_active_ver, "due_in": due,
        "current": device.hw_current or {}, "wanted": device.hw_profile or {},
    }
