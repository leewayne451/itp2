"""Admin identity management and IT-admin department->service mapping."""
from flask import Blueprint, request, redirect, url_for, flash, render_template

from . import role_required
from ..backend_client import call, error_of

bp = Blueprint("admin", __name__)


def _back():
    return redirect(request.referrer or url_for("dashboard.index"))


@bp.post("/employees")
@role_required("admin")
def create_employee():
    payload = {
        "full_name": request.form.get("full_name", "").strip(),
        "username": request.form.get("username", "").strip(),
        "password": request.form.get("password", ""),
        "email": request.form.get("email", "").strip(),
        "role": request.form.get("role", "employee"),
        "department_id": int(request.form["department_id"]) if request.form.get("department_id") else None,
    }
    resp = call("POST", "/api/employees", json=payload)
    if not resp.ok:
        flash(error_of(resp), "error")
        return _back()
    temp = resp.json().get("temporary_password")
    if temp:   # generated: show it ONCE, on this response only (never stored in a cookie)
        return render_template("temporary_password.html", username=payload["username"],
                               full_name=payload["full_name"], password=temp, created=True)
    flash("Account created. The user must change the initial password at first sign-in.", "ok")
    return _back()


@bp.post("/employees/<int:emp_id>/password/reset")
@role_required("admin")
def reset_password(emp_id):
    resp = call("POST", f"/api/employees/{emp_id}/password/reset", json={})
    if not resp.ok:
        flash(error_of(resp), "error")
        return _back()
    return render_template("temporary_password.html", username=request.form.get("username", ""),
                           full_name=request.form.get("full_name", ""), password=resp.json()["temporary_password"],
                           created=False)


@bp.post("/employees/<int:emp_id>/sessions/revoke")
@role_required("admin")
def revoke_sessions(emp_id):
    resp = call("POST", f"/api/employees/{emp_id}/sessions/revoke", json={})
    if not resp.ok:
        flash(error_of(resp), "error")
    elif resp.json().get("keycloak_synced") is False:
        flash("Signed out of the console and services. Keycloak is unreachable, so its sign-in session "
              "will be ended automatically when it is back.", "ok")
    else:
        flash("Signed out everywhere.", "ok")
    return _back()


@bp.post("/employees/<int:emp_id>")
@role_required("admin")
def update_employee(emp_id):
    body = {}
    if "email" in request.form:
        body["email"] = request.form.get("email", "").strip()
    if request.form.get("role"):
        body["role"] = request.form["role"]
    if "department_id" in request.form:
        body["department_id"] = int(request.form["department_id"]) if request.form.get("department_id") else None
    if request.form.get("status"):
        body["status"] = request.form["status"]
    # Rotation clock start (CHANGE 006): only sent when the administrator changed it.
    if "department_since" in request.form and \
            request.form.get("department_since", "") != request.form.get("department_since_was", ""):
        body["department_since"] = request.form.get("department_since", "").strip() or None
    resp = call("PUT", f"/api/employees/{emp_id}", json=body)
    flash("Updated." if resp.ok else error_of(resp), "ok" if resp.ok else "error")
    return _back()


@bp.post("/employees/<int:emp_id>/delete")
@role_required("admin")
def delete_employee(emp_id):
    resp = call("DELETE", f"/api/employees/{emp_id}")
    flash("Deleted." if resp.ok else error_of(resp), "ok" if resp.ok else "error")
    return _back()


@bp.post("/employees/<int:emp_id>/mfa/reset")
@role_required("admin")
def reset_mfa(emp_id):
    resp = call("POST", f"/api/employees/{emp_id}/mfa/reset", json={})
    flash("Two-factor authentication reset — the user will set it up again at next sign-in."
          if resp.ok else error_of(resp), "ok" if resp.ok else "error")
    return _back()


@bp.post("/departments")
@role_required("it_admin", "admin")
def create_department():
    resp = call("POST", "/api/departments", json={"name": request.form.get("name", "").strip()})
    flash("Department created." if resp.ok else error_of(resp), "ok" if resp.ok else "error")
    return _back()


@bp.post("/departments/<int:dept_id>/quotas")
@role_required("it_admin", "admin")
def set_department_quotas(dept_id):
    """IT Admin sets this department's laptop/account capacity."""
    quotas = {key[len("quota_"):]: val.strip()
              for key, val in request.form.items() if key.startswith("quota_") and val.strip() != ""}
    if not quotas:
        flash("Enter a quota value.", "error")
        return _back()
    resp = call("PUT", f"/api/departments/{dept_id}/quotas", json={"quotas": quotas})
    flash("Department quota saved." if resp.ok else error_of(resp), "ok" if resp.ok else "error")
    return _back()


@bp.post("/departments/<int:dept_id>/access")
@role_required("it_admin", "admin")
def set_department_access(dept_id):
    service_ids = [int(s) for s in request.form.getlist("service_ids")]
    resp = call("PUT", f"/api/departments/{dept_id}/access", json={"service_ids": service_ids})
    flash("Department mapping saved." if resp.ok else error_of(resp), "ok" if resp.ok else "error")
    return _back()
