"""Firmware files: list, add / edit / delete (admin), info, flash a meter over the air or by USB from the browser
(docs/protocol.md §14)."""
from flask import (Blueprint, abort, flash, jsonify, redirect, render_template, request, send_file, url_for)
from flask_login import current_user

from ..extensions import db
from ..models import Device, Firmware, OtaJob, now
from ..services import access, firmware as fw_svc, hub
from .decorators import admin_required, staff_required

bp = Blueprint("fw", __name__)


def _fw(fid):
    fw = db.session.get(Firmware, fid)
    if fw is None:
        abort(404)
    return fw


def _meters():
    return access.managed_devices(current_user).all()


def _clean(form):
    return {k: (form.get(k) or "").strip() for k in ("title", "info", "description", "version")}


@bp.route("/firmware/add", methods=["GET", "POST"])
@admin_required
def add():
    errors, vals = [], _clean(request.form)
    if request.method == "POST":
        f = request.files.get("zip")
        if not vals["title"]:
            errors.append("Title is needed.")
        if f is None or not f.filename:
            errors.append("Choose the .zip file.")
        info = files = None
        if not errors:
            info, files, err = fw_svc.parse_zip(f.read())
            if err:
                errors.append(err)
        if not errors:
            fw = Firmware(title=vals["title"][:40], info=vals["info"][:60], description=vals["description"][:4000],
                          version=(vals["version"] or info["version"])[:24], fw_type=info["fw_type"], hw=info["hw"],
                          chip=info["chip"], size=info["size"], sha256=info["sha256"], layout=info["layout"],
                          files=info["files"], updated_by=current_user.username)
            db.session.add(fw)
            db.session.commit()   # a short write; the files (several MB) are written without holding the database lock
            try:
                fw_svc.store(fw, files)
            except OSError as exc:
                db.session.delete(fw)
                db.session.commit()
                fw_svc.remove_files(fw.id)
                errors.append(f"The file could not be saved on the server ({exc.strerror or exc}).")
            else:
                flash(f"Firmware {fw.title} {fw.version} added.", "success")
                return redirect(url_for("main.firmware"))
    return render_template("firmware_form.html", fw=None, errors=errors, vals=vals)


@bp.route("/firmware/<int:fid>/edit", methods=["GET", "POST"])
@admin_required
def edit(fid):
    fw = _fw(fid)
    errors, vals = [], {"title": fw.title, "info": fw.info, "description": fw.description, "version": fw.version}
    if request.method == "POST":
        vals = _clean(request.form)
        if not vals["title"]:
            errors.append("Title is needed.")
        f = request.files.get("zip")
        info = files = None
        if not errors and f is not None and f.filename:
            info, files, err = fw_svc.parse_zip(f.read())
            if err:
                errors.append(err)
            elif info["fw_type"] != fw.fw_type:
                errors.append(f"The new file is a different firmware type ({info['fw_type']}, this entry is {fw.fw_type}). Add it as a new firmware.")
        if not errors:
            fw.title, fw.info, fw.description = vals["title"][:40], vals["info"][:60], vals["description"][:4000]
            fw.version = (vals["version"] or fw.version)[:24]
            if info:
                fw.hw, fw.chip, fw.size, fw.sha256, fw.layout, fw.files = (info["hw"], info["chip"], info["size"], info["sha256"],
                                                                         info["layout"], info["files"])
                if not vals["version"]:
                    fw.version = info["version"]
                fw_svc.store(fw, files)
            fw.updated_at, fw.updated_by = now(), current_user.username
            db.session.commit()
            flash("Firmware saved.", "success")
            return redirect(url_for("main.firmware"))
    return render_template("firmware_form.html", fw=fw, errors=errors, vals=vals)


@bp.post("/firmware/delete")
@admin_required
def delete():
    ids = [int(i) for i in request.form.getlist("ids") if i.isdigit()]
    busy = {j.firmware_fk for j in OtaJob.query.filter(OtaJob.state.in_(fw_svc.ACTIVE)).all()}
    n = 0
    for fw in Firmware.query.filter(Firmware.id.in_(ids)).all() if ids else []:
        if fw.id in busy:
            flash(f"{fw.title} is being flashed and cannot be deleted now.", "warning")
            continue
        fw_svc.remove_files(fw.id)
        OtaJob.query.filter_by(firmware_fk=fw.id).update({"firmware_fk": None})  # the history keeps the title
        db.session.delete(fw)
        n += 1
    db.session.commit()
    if n:
        flash(f"{n} firmware deleted.", "success")
    return redirect(url_for("main.firmware"))


@bp.get("/firmware/<int:fid>")
@staff_required
def info(fid):
    return render_template("firmware_info.html", fw=_fw(fid))


@bp.get("/firmware/<int:fid>/flash")
@staff_required
def flash_page(fid):
    fw = _fw(fid)
    meters = sorted(_meters(), key=lambda d: (d.name or "").lower())
    pre = request.args.get("meter", type=int)
    return render_template("firmware_flash.html", fw=fw, pre=pre,
                           meters=[{"id": d.id, "name": d.name, "device_id": d.device_id, "fw": d.fw, "online": hub.is_online(d.id)}
                                   for d in meters])


# ---- over the air
@bp.get("/firmware/<int:fid>/check")
@staff_required
def check(fid):
    fw = _fw(fid)
    d = access.device_for_manager(request.args.get("meter", type=int) or 0)
    return jsonify(ok=True, **fw_svc.verdict(fw, d))


@bp.post("/firmware/<int:fid>/ota")
@staff_required
def start(fid):
    fw = _fw(fid)
    body = request.get_json(silent=True) or {}
    d = access.device_for_manager(int(body.get("meter") or 0))
    v = fw_svc.verdict(fw, d)
    if not body.get("confirm"):
        return jsonify(ok=False, error="Tick the box to confirm."), 400
    job, err = fw_svc.start(fw, d, current_user)
    if err:
        return jsonify(ok=False, error=err), 409
    return jsonify(ok=True, job=fw_svc.job_view(job))


@bp.get("/firmware/job/<int:jid>")
@staff_required
def job_status(jid):
    job = db.session.get(OtaJob, jid)
    if job is None:
        abort(404)
    access.device_for_manager(job.device_fk)  # 404 / 403 for a meter the user does not manage
    d = db.session.get(Device, job.device_fk)
    return jsonify(ok=True, job=fw_svc.job_view(job), fw=d.fw, online=hub.is_online(d.id))


# ---- USB flash from the browser (Web Serial): the files and a log entry
@bp.get("/firmware/<int:fid>/usb/manifest")
@staff_required
def usb_manifest(fid):
    fw = _fw(fid)
    parts = [dict(p, url=url_for("fw.usb_file", fid=fw.id, name=p["name"])) for p in (fw.files or {}).get("parts", [])]
    return jsonify(ok=True, title=fw.title, version=fw.version, fw_type=fw.fw_type, hw=fw.hw, chip=fw.chip,
                   flash=(fw.files or {}).get("flash") or {}, parts=parts)


@bp.get("/firmware/<int:fid>/usb/file/<name>")
@staff_required
def usb_file(fid, name):
    fw = _fw(fid)
    path = fw_svc.file_path(fw, name)
    if path is None:
        abort(404)
    return send_file(path, mimetype="application/octet-stream", as_attachment=False, max_age=0)


@bp.post("/firmware/<int:fid>/usb/log")
@staff_required
def usb_log(fid):
    """The browser reports a finished USB flash (audit log)."""
    import logging
    fw = _fw(fid)
    body = request.get_json(silent=True) or {}
    logging.getLogger("audit").info("USB flash by %s: firmware %s %s (%s) chip %s mac %s erase=%s ok=%s",
                                    current_user.username, fw.title, fw.version, fw.fw_type, str(body.get("chip"))[:40],
                                    str(body.get("mac"))[:20], bool(body.get("erase")), bool(body.get("ok")))
    return jsonify(ok=True)
