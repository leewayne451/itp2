"""Mandatory Job Rotation (MJR) tracker  (CHANGE 006): Director decisions and HR's
"check now". The lists themselves are rendered on the role dashboards."""
from flask import Blueprint, request, redirect, url_for, flash

from . import role_required
from ..backend_client import call, error_of

bp = Blueprint("rotations", __name__)


def _back():
    return redirect(request.referrer or url_for("dashboard.index"))


@bp.post("/rotations/<int:emp_id>/rotate")
@role_required("director")
def rotate(emp_id):
    dept = request.form.get("department_id")
    resp = call("POST", f"/api/rotations/{emp_id}/decision", json={
        "decision": "rotate", "department_id": int(dept) if dept else None,
        "reason": request.form.get("reason", "").strip()})
    flash("Rotation confirmed — the Requestor has been asked to raise the department shift."
          if resp.ok else error_of(resp), "ok" if resp.ok else "error")
    return _back()


@bp.post("/rotations/<int:emp_id>/keep")
@role_required("director")
def keep(emp_id):
    resp = call("POST", f"/api/rotations/{emp_id}/decision", json={
        "decision": "keep", "reason": request.form.get("reason", "").strip()})
    flash("Officer kept in the department — reason recorded." if resp.ok else error_of(resp),
          "ok" if resp.ok else "error")
    return _back()


@bp.post("/rotations/check")
@role_required("hr")
def check():
    resp = call("POST", "/api/rotations/check", json={})
    if resp.ok:
        n = (resp.json() or {}).get("notified", 0)
        flash(f"Directors e-mailed about {n} officer(s) newly due." if n
              else "No officers newly due — Directors have already been told about everyone due.", "ok")
    else:
        flash(error_of(resp), "error")
    return _back()
