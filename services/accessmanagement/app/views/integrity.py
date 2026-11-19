"""Mandatory Block Leave (leave clearance) + casino visit declarations  (CHANGE 012):
HR leave actions, IT Admin's sensitive-position switch, and the officer's own declaration.
The lists are rendered on the Director / HR dashboard, under the MJR panels."""
from flask import Blueprint, request, redirect, url_for, flash, g

from . import login_required, role_required
from ..backend_client import call, error_of

bp = Blueprint("integrity", __name__)


def _back(anchor=""):
    return redirect((request.referrer or url_for("dashboard.index")).split("#")[0] + anchor)


def _done(resp, ok_message, anchor):
    flash(ok_message if resp.ok else error_of(resp), "ok" if resp.ok else "error")
    return _back(anchor)


@bp.post("/integrity/officers/<int:emp_id>/sensitive")
@role_required("it_admin")
def set_sensitive(emp_id):
    on = request.form.get("sensitive") == "yes"
    resp = call("POST", f"/api/integrity/officers/{emp_id}/sensitive",
                json={"sensitive": on, "reason": request.form.get("reason", "")})
    return _done(resp, "Marked as a sensitive position." if on else "No longer a sensitive position.", "#sensitive")


@bp.post("/mbl/entitlements")
@role_required("hr")
def set_entitlement():
    resp = call("POST", "/api/mbl/entitlements", json={
        "employee_id": request.form.get("employee_id"), "year": request.form.get("year"),
        "entitled_days": request.form.get("entitled_days"),
        "carried_forward_days": request.form.get("carried_forward_days") or 0})
    return _done(resp, "Leave entitlement updated.", "#mbl")


@bp.post("/mbl/leaves")
@role_required("hr")
def record_leave():
    resp = call("POST", "/api/mbl/leaves", json={
        "employee_id": request.form.get("employee_id"), "start_date": request.form.get("start_date"),
        "end_date": request.form.get("end_date"), "note": request.form.get("note", "")})
    if resp.ok:
        r = resp.json()
        flash(f"Leave recorded: {r['working_days']} working day(s). {r['remaining']} day(s) still to clear this year."
              if r["remaining"] else f"Leave recorded: {r['working_days']} working day(s). All leave is now cleared or booked.",
              "ok")
        return _back("#mbl")
    return _done(resp, "", "#mbl")


@bp.post("/mbl/leaves/<int:record_id>/cancel")
@role_required("hr")
def cancel_leave(record_id):
    return _done(call("POST", f"/api/mbl/leaves/{record_id}/cancel", json={}), "Leave record cancelled.", "#mbl")


@bp.post("/mbl/remind")
@role_required("hr")
def remind():
    resp = call("POST", "/api/mbl/remind", json={})
    if resp.ok:
        n = resp.json().get("reminded", 0)
        flash(f"Reminder e-mailed to {n} officer(s) with leave still to clear (copy to HR)." if n
              else "Every officer has cleared or booked all their leave.", "ok")
        return _back("#mbl")
    return _done(resp, "", "#mbl")


@bp.post("/casino/declarations")
@login_required
def declare():
    body = {"kind": request.form.get("kind") or "visit", "visit_date": request.form.get("visit_date"),
            "casino": request.form.get("casino"), "official_duty": request.form.get("official_duty") == "on",
            "purpose": request.form.get("purpose", "")}
    if g.user.get("role") == "hr":
        body["employee_id"] = request.form.get("employee_id")
    resp = call("POST", "/api/casino/declarations", json=body)
    if resp.ok:
        d = resp.json()
        if "late" in d["flags"]:
            flash(f"Declaration recorded, but it is late (it was due by {d['deadline']}). HR has been told.", "error")
        else:
            flash("Declaration recorded.", "ok")
        return _back("#casino")
    return _done(resp, "", "#casino")
