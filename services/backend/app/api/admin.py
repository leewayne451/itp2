"""Identity + configuration: employee CRUD, department->service mapping, assets."""
from datetime import date

from flask import Blueprint, request, jsonify, g

from .. import audit, db, keycloak, passwords, sync
from ..auth import require_role
from ..config import MFA_REQUIRED_ROLES
from ..core import VALID_ROLES

ADMIN_SETTABLE_STATUS = ("active", "inactive")   # 'suspended' is applied only by the shift workflow

# NOTE: there is deliberately NO direct suspend/resume endpoint here. Suspension is
# a request type that goes through the same chain as everything else and is applied
# by EUSS — see app/api/requests.py.

bp = Blueprint("admin_api", __name__)


# ------------------------------------------------------------ employees (admin)
def _kc_error(e):
    return jsonify(error=f"Keycloak: {e.message}"), (400 if e.status == 400 else 503)


def _is_system(emp_id):
    return db.query("SELECT 1 FROM employees WHERE id=%s AND role='system'", (emp_id,), fetch="one") is not None


def _linked(emp_id):
    """(employee, keycloak_id) — syncing first if the account is not in Keycloak yet."""
    emp = db.query("SELECT * FROM employees WHERE id=%s", (emp_id,), fetch="one")
    if not emp:
        return None, None
    kc_id = emp["keycloak_id"] or sync.sync_employee(emp_id)
    return emp, kc_id


@bp.post("/api/employees")
@require_role("admin")
def create_employee():
    """Create an employee here AND their Keycloak account, with a TEMPORARY password
    Keycloak forces them to replace at first sign-in. Leave `password` blank to have a
    strong one generated (returned once). Keycloak enforces the password policy."""
    body = request.get_json(silent=True) or {}
    username = (body.get("username") or "").strip()
    password = body.get("password") or ""
    full_name = (body.get("full_name") or "").strip()
    email = (body.get("email") or "").strip() or None
    role = body.get("role") or "employee"
    department_id = body.get("department_id")
    if not username or not full_name:
        return jsonify(error="username and full_name are required"), 400
    if role not in VALID_ROLES:
        return jsonify(error="Invalid role"), 400
    if db.query("SELECT 1 FROM employees WHERE username=%s", (username,), fetch="one"):
        return jsonify(error="Username already taken"), 409
    generated = not password
    if generated:
        password = passwords.generate_temporary_password()
    row = db.query(
        """INSERT INTO employees (username, full_name, email, role, department_id, is_admin, kc_sync_pending,
                                  department_since)
           VALUES (%s,%s,%s,%s,%s,%s,TRUE, CASE WHEN %s IS NULL THEN NULL ELSE CURRENT_DATE END) RETURNING id""",
        (username, full_name, email, role, department_id, role == "admin", department_id), fetch="one")
    try:
        kc_id = sync.sync_employee(row["id"])
        keycloak.set_temporary_password(kc_id, password)
    except keycloak.KeycloakError as e:
        # Keep the two systems consistent: no half-created account.
        emp = db.query("SELECT keycloak_id FROM employees WHERE id=%s", (row["id"],), fetch="one")
        if emp and emp["keycloak_id"]:
            sync.delete(emp["keycloak_id"])
        db.query("DELETE FROM employees WHERE id=%s", (row["id"],), fetch=None)
        return _kc_error(e)
    audit.record("admin", "account_created", target_type="employee", target_id=row["id"], target_label=username,
                 details={"full_name": full_name, "role": role, "email": email,
                          "department": _dept_name(department_id), "two_factor_required": role in MFA_REQUIRED_ROLES,
                          "password": "temporary (generated)" if generated else "temporary (set by administrator)"})
    return jsonify(id=row["id"], temporary_password=password if generated else None), 201


@bp.put("/api/employees/<int:emp_id>")
@require_role("admin")
def update_employee(emp_id):
    body = request.get_json(silent=True) or {}
    if not db.query("SELECT 1 FROM employees WHERE id=%s", (emp_id,), fetch="one"):
        return jsonify(error="Employee not found"), 404
    if _is_system(emp_id):
        return jsonify(error="The HR-system account is managed by the platform"), 403
    # Passwords are deliberately NOT editable here: use the reset endpoint.
    if "password" in body:
        return jsonify(error="Use POST /api/employees/<id>/password/reset to reset a password"), 400
    if "status" in body and body["status"] not in ADMIN_SETTABLE_STATUS:
        return jsonify(error=f"status must be one of {', '.join(ADMIN_SETTABLE_STATUS)}"), 400
    before = db.query("SELECT username, full_name, email, role, status, department_id, "
                      "to_char(department_since, 'YYYY-MM-DD') AS department_since FROM employees WHERE id=%s",
                      (emp_id,), fetch="one")
    fields, values = [], []
    for col in ("full_name", "email", "department_id", "status"):
        if col in body:
            fields.append(f"{col}=%s")
            values.append(body[col])
    # Mandatory Job Rotation clock (CHANGE 006): a direct department move restarts it;
    # an administrator may also correct the start date (e.g. from HR records).
    if "department_since" in body:
        since = (body.get("department_since") or "").strip() or None
        if since:
            try:
                parsed = date.fromisoformat(since)
            except ValueError:
                return jsonify(error="department_since must be a date (YYYY-MM-DD)"), 400
            if parsed > date.today():
                return jsonify(error="department_since cannot be in the future"), 400
            since = parsed.isoformat()
        body["department_since"] = since
        fields.append("department_since=%s"); values.append(since)
        fields.append("rotation_alerted_at=NULL")
    elif "department_id" in body and body["department_id"] != before["department_id"]:
        fields.append("department_since=" + ("CURRENT_DATE" if body["department_id"] else "NULL"))
        fields.append("rotation_alerted_at=NULL")
    if "role" in body:
        if body["role"] not in VALID_ROLES:
            return jsonify(error="Invalid role"), 400
        fields.append("role=%s"); values.append(body["role"])
        fields.append("is_admin=%s"); values.append(body["role"] == "admin")
    if not fields:
        return jsonify(error="Nothing to update"), 400
    values.append(emp_id)
    db.query(f"UPDATE employees SET {', '.join(fields)} WHERE id=%s", tuple(values), fetch=None)
    # A deactivation or a change of privileges takes effect immediately: the user is
    # signed out of every app (Keycloak) and earlier tokens are refused (API).
    reasons = []
    if body.get("status") == "inactive" and before["status"] != "inactive":
        reasons.append("account deactivated")
    if "role" in body and body["role"] != before["role"]:
        reasons.append(f"role changed {before['role']} -> {body['role']}")
    synced = sync.push(emp_id, end_sessions=bool(reasons))
    # Audit exactly what changed, old -> new (department ids shown as names).
    changes = {}
    for col in ("full_name", "email", "role", "status", "department_id", "department_since"):
        if col in body and body[col] != before[col]:
            old_v, new_v = before[col], body[col]
            if col == "department_id":
                col, old_v, new_v = "department", _dept_name(old_v), _dept_name(new_v)
            changes[col] = {"from": old_v, "to": new_v}
    if changes:
        details = {"changes": changes}
        if reasons:
            details["signed_out_everywhere"] = True
        if not synced:
            details["keycloak_sync"] = "pending (Keycloak unreachable; will retry)"
        audit.record("admin", "account_updated", target_type="employee", target_id=emp_id,
                     target_label=before["username"], details=details)
    return jsonify(ok=True, keycloak_synced=synced)


@bp.post("/api/employees/<int:emp_id>/password/reset")
@require_role("admin")
def admin_reset_password(emp_id):
    """Help-desk reset: a random temporary password (shown ONCE to the administrator)
    is set in Keycloak, which forces a change at next sign-in; any brute-force
    lock-out is lifted and the user is signed out everywhere."""
    try:
        emp, kc_id = _linked(emp_id)
        if not emp:
            return jsonify(error="Employee not found"), 404
        # Refuse the user's existing tokens and service sessions FIRST, so this takes
        # effect in the API even if Keycloak cannot be reached for the steps below.
        sync.revoke_tokens(emp_id)
        temp = passwords.generate_temporary_password()
        keycloak.set_temporary_password(kc_id, temp)
        keycloak.clear_lockout(kc_id)
        keycloak.logout(kc_id)
    except keycloak.KeycloakError as e:
        return _kc_error(e)
    audit.record("admin", "password_reset_by_admin", target_type="employee", target_id=emp_id,
                 target_label=emp["username"], details={"temporary_password_issued": True,
                                                        "signed_out_everywhere": True})
    return jsonify(ok=True, temporary_password=temp)


@bp.post("/api/employees/<int:emp_id>/mfa/reset")
@require_role("admin")
def admin_reset_mfa(emp_id):
    """Lost phone: remove the user's authenticator from Keycloak and sign them out.
    Staff are made to set up a new one at their next sign-in."""
    try:
        emp, kc_id = _linked(emp_id)
        if not emp:
            return jsonify(error="Employee not found"), 404
        sync.revoke_tokens(emp_id)          # first — see admin_reset_password
        removed = keycloak.remove_otp(kc_id)
        keycloak.logout(kc_id)
    except keycloak.KeycloakError as e:
        return _kc_error(e)
    audit.record("admin", "mfa_reset", target_type="employee", target_id=emp_id, target_label=emp["username"],
                 details={"authenticators_removed": removed, "signed_out_everywhere": True})
    return jsonify(ok=True, removed=removed)


@bp.post("/api/employees/<int:emp_id>/sessions/revoke")
@require_role("admin")
def admin_revoke_sessions(emp_id):
    """Force sign-out: refuses any access token / service session already issued
    (immediately, in the API) and ends the user's Keycloak sessions (every app, via
    back-channel logout). If Keycloak is down the Keycloak part is queued and
    retried in the background; the API-side revocation applies regardless."""
    emp = db.query("SELECT id, username FROM employees WHERE id=%s", (emp_id,), fetch="one")
    if not emp:
        return jsonify(error="Employee not found"), 404
    sync.revoke_tokens(emp_id)
    sync.request_logout(emp_id)
    try:
        sync.sync_employee(emp_id)          # performs (and clears) the pending Keycloak logout
        synced = True
    except keycloak.KeycloakError as e:
        print(f"[admin] sign-out of {emp['username']} queued for Keycloak: {e.message}", flush=True)
        synced = False
    details = {"signed_out_everywhere": True}
    if not synced:
        details["keycloak_sync"] = "pending (Keycloak unreachable; will retry)"
    audit.record("admin", "sessions_revoked", target_type="employee", target_id=emp_id,
                 target_label=emp["username"], details=details)
    return jsonify(ok=True, keycloak_synced=synced)


@bp.delete("/api/employees/<int:emp_id>")
@require_role("admin")
def delete_employee(emp_id):
    emp = db.query("SELECT username, full_name, role, status, keycloak_id FROM employees WHERE id=%s",
                   (emp_id,), fetch="one")
    if not emp:
        return jsonify(error="Employee not found"), 404
    if emp["role"] == "system":
        return jsonify(error="The HR-system account is managed by the platform"), 403
    db.query("DELETE FROM employees WHERE id=%s", (emp_id,), fetch=None)
    try:
        sync.delete(emp["keycloak_id"])     # removes the login (and ends its sessions)
    except keycloak.KeycloakError as e:
        print(f"[admin] Keycloak account for {emp['username']} not deleted: {e.message}", flush=True)
    # The audit trail has no foreign keys, so this entry (and all earlier ones
    # about this person) survive the deletion.
    audit.record("admin", "account_deleted", target_type="employee", target_id=emp_id, target_label=emp["username"],
                 details={"full_name": emp["full_name"], "role": emp["role"], "status": emp["status"]})
    return jsonify(deleted=emp_id)


# ---------------------------------------------------- department->service mapping
@bp.post("/api/departments")
@require_role("it_admin", "admin")
def create_department():
    name = ((request.get_json(silent=True) or {}).get("name") or "").strip()
    if not name:
        return jsonify(error="Department name required"), 400
    if db.query("SELECT 1 FROM departments WHERE name=%s", (name,), fetch="one"):
        return jsonify(error="Department already exists"), 409
    row = db.query("INSERT INTO departments (name) VALUES (%s) RETURNING id", (name,), fetch="one")
    audit.record("config", "department_created", target_type="department", target_id=row["id"], target_label=name)
    return jsonify(id=row["id"], name=name, access=[]), 201


@bp.put("/api/departments/<int:dept_id>/access")
@require_role("it_admin", "admin")
def set_department_access(dept_id):
    """IT Admin oversees which services a department grants (applied on the next
    onboard/shift into that department, not retroactively)."""
    service_ids = (request.get_json(silent=True) or {}).get("service_ids") or []
    dept = db.query("SELECT name FROM departments WHERE id=%s", (dept_id,), fetch="one")
    if not dept:
        return jsonify(error="Department not found"), 404
    before = _service_names(r["service_id"] for r in
                            db.query("SELECT service_id FROM department_access WHERE department_id=%s", (dept_id,)))
    conn = db.get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM department_access WHERE department_id=%s", (dept_id,))
            for sid in service_ids:
                cur.execute("INSERT INTO department_access (department_id, service_id) VALUES (%s,%s)", (dept_id, sid))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    rows = db.query("SELECT service_id FROM department_access WHERE department_id=%s", (dept_id,))
    after = _service_names(r["service_id"] for r in rows)
    if before != after:
        audit.record("config", "department_services_changed", target_type="department", target_id=dept_id,
                     target_label=dept["name"], details={"from": before, "to": after})
    return jsonify(id=dept_id, access=[r["service_id"] for r in rows])


def _dept_name(dept_id):
    if dept_id in (None, ""):
        return None
    row = db.query("SELECT name FROM departments WHERE id=%s", (dept_id,), fetch="one")
    return row["name"] if row else f"#{dept_id}"


def _service_names(ids):
    ids = list(ids)
    if not ids:
        return []
    return [r["name"] for r in db.query("SELECT name FROM services WHERE id = ANY(%s) ORDER BY id", (ids,))]


# ------------------------------------------------------------------------ assets
@bp.get("/api/assets")
@require_role("admin", "euss")
def list_assets():
    return jsonify(db.query("""
        SELECT a.id, a.asset_tag, a.kind, a.status, a.assigned_to,
               e.full_name AS assigned_name, d.name AS assigned_dept
        FROM assets a
        LEFT JOIN employees e ON e.id = a.assigned_to
        LEFT JOIN departments d ON d.id = e.department_id
        ORDER BY a.kind, a.asset_tag
    """))


@bp.get("/api/assets/<int:asset_id>/history")
@require_role("euss", "admin")
def asset_history(asset_id):
    asset = db.query("""SELECT a.asset_tag, a.kind, a.status, e.full_name AS holder, d.name AS holder_dept
                        FROM assets a LEFT JOIN employees e ON e.id = a.assigned_to
                        LEFT JOIN departments d ON d.id = e.department_id WHERE a.id=%s""", (asset_id,), fetch="one")
    if not asset:
        return jsonify(error="Asset not found"), 404
    events = db.query("""SELECT ae.action, ae.created_at, em.full_name AS employee_name, dp.name AS dept
                         FROM asset_events ae
                         LEFT JOIN employees em ON em.id = ae.employee_id
                         LEFT JOIN departments dp ON dp.id = em.department_id
                         WHERE ae.asset_id=%s ORDER BY ae.created_at, ae.id""", (asset_id,))
    return jsonify(
        asset={"asset_tag": asset["asset_tag"], "kind": asset["kind"], "status": asset["status"],
               "current_holder": asset["holder"], "current_dept": asset["holder_dept"]},
        events=[{"action": e["action"], "at": e["created_at"].isoformat(),
                 "employee_name": e["employee_name"], "dept": e["dept"]} for e in events])
