"""HR/IT console actions for leaver cut-off."""
from flask import Blueprint, flash, redirect, request, url_for

from . import role_required
from ..backend_client import call, error_of

bp = Blueprint("offboarding", __name__)


def _back():
    return redirect(request.referrer or url_for("dashboard.index"))


@bp.post("/employees/<int:emp_id>/offboarding/schedule")
@role_required("hr", "it_admin")
def schedule(emp_id):
    resp = call("POST", f"/api/employees/{emp_id}/offboarding/schedule", json={
        "effective_at": request.form.get("effective_at", ""),
        "reason": request.form.get("reason", "").strip(),
    })
    flash("Offboarding scheduled." if resp.ok else error_of(resp), "ok" if resp.ok else "error")
    return _back()


@bp.post("/employees/<int:emp_id>/offboarding/cancel")
@role_required("hr", "it_admin")
def cancel(emp_id):
    resp = call("DELETE", f"/api/employees/{emp_id}/offboarding/schedule", json={
        "reason": request.form.get("reason", "").strip(),
    })
    flash("Scheduled offboarding cancelled." if resp.ok else error_of(resp),
          "ok" if resp.ok else "error")
    return _back()


@bp.post("/employees/<int:emp_id>/offboard")
@role_required("it_admin")
def emergency(emp_id):
    resp = call("POST", f"/api/employees/{emp_id}/offboard", json={
        "reason": request.form.get("reason", "").strip(),
    })
    if resp.ok and resp.json().get("already_offboarded"):
        message = "Employee was already offboarded; no duplicate action was created."
    else:
        message = "Emergency kill switch applied. Sessions ended and an automated Return request was raised for equipment recovery."
    flash(message if resp.ok else error_of(resp), "ok" if resp.ok else "error")
    return _back()
