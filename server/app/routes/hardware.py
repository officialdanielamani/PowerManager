"""Update Device I/O (Firmware page): the hardware profile of an rbAmp meter, docs/protocol.md §12."""
from flask import Blueprint, abort, jsonify, render_template, request
from flask_login import current_user

from ..extensions import db
from ..models import Command
from ..services import access, cfggen, hub, hwprofile
from .decorators import staff_required

bp = Blueprint("hardware", __name__)


def _meter(pk):
    d = access.device_for_manager(pk)
    if not d.is_mc:
        abort(404)
    return d


def _eligible():
    """Meters the user manages that can take a hardware profile (multi-channel rbAmp firmware)."""
    return [d for d in access.managed_devices(current_user).all() if d.is_mc]


@bp.get("/firmware/io")
@staff_required
def page():
    meters = _eligible()
    pk = request.args.get("meter", type=int)
    meter = next((d for d in meters if d.id == pk), None)
    return render_template("firmware_io.html", meters=meters, meter=meter, online=hub.is_online(meter.id) if meter else False,
                           hw=hwprofile, view=hwprofile.view(meter) if meter else None)


@bp.get("/firmware/io/<int:pk>/status")
@staff_required
def status(pk):
    d = _meter(pk)
    return jsonify(ok=True, online=hub.is_online(d.id), **hwprofile.view(d))


def _cmd_json(cmd, err):
    if err:
        msg = {"offline": "The meter is offline: scanning and the address change need a connected meter."}.get(
            err if isinstance(err, str) else "", err if isinstance(err, str) else "; ".join(err))
        return jsonify(ok=False, error=msg), 409
    return jsonify(ok=True, cmd=cmd.id if cmd else None)


@bp.post("/firmware/io/<int:pk>/scan")
@staff_required
def scan(pk):
    d = _meter(pk)
    cmd, err = hwprofile.scan(d, current_user)
    return _cmd_json(cmd, err)


@bp.get("/firmware/io/<int:pk>/cmd/<int:cid>")
@staff_required
def command_result(pk, cid):
    d = _meter(pk)
    cmd = db.session.get(Command, cid)
    if cmd is None or cmd.device_fk != d.id:
        abort(404)
    return jsonify(ok=True, done=cmd.acked_at is not None, success=cmd.ok, err=cmd.err, res=cmd.res or {},
                   sent=cmd.sent_at is not None)


@bp.post("/firmware/io/<int:pk>/addr")
@staff_required
def set_address(pk):
    d = _meter(pk)
    body = request.get_json(silent=True) or {}
    old, new = body.get("from"), body.get("to")
    if not isinstance(old, int) or not isinstance(new, int):
        return jsonify(ok=False, error="Choose the old and the new address"), 400
    cmd, errors = hwprofile.set_rbamp_address(d, old, new, current_user)
    return _cmd_json(cmd, errors)


@bp.post("/firmware/io/<int:pk>/apply")
@staff_required
def apply(pk):
    d = _meter(pk)
    body = request.get_json(silent=True) or {}
    mode = body.get("mode")
    if mode not in ("live", "reboot"):
        return jsonify(ok=False, errors=["Unknown mode"]), 400
    cmd, errors = hwprofile.submit(d, body.get("profile"), current_user, mode=mode, delay_s=body.get("delay_s", 60))
    if errors:
        return jsonify(ok=False, errors=errors), 400
    return jsonify(ok=True, mode=cmd.params["mode"], ver=cmd.params["ver"], queued=cmd.sent_at is None)


@bp.post("/firmware/io/<int:pk>/cancel")
@staff_required
def cancel(pk):
    d = _meter(pk)
    cmd, err = hwprofile.cancel(d, current_user)
    if err:
        return jsonify(ok=False, errors=[err]), 409
    return jsonify(ok=True)


@bp.get("/firmware/config/from-meter/<int:pk>")
@staff_required
def generator_profile(pk):
    """Generator form values from a meter's current hardware profile."""
    d = _meter(pk)
    prof = d.hw_current if (d.hw_current or {}).get("ch") else d.hw_profile
    if not (prof or {}).get("ch"):
        return jsonify(ok=False, error="This meter has not reported its hardware profile yet (needs firmware 1.2.0)."), 404
    return jsonify(ok=True, name=d.name, **cfggen.from_profile(prof))
