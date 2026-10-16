"""Identity + configuration: employee CRUD, department->service mapping, assets."""
from flask import Blueprint, request, jsonify

from .. import db
from ..auth import require_role
from ..core import VALID_ROLES

# NOTE: there is deliberately NO direct suspend/resume endpoint here. Suspension is
# a request type that goes through the same chain as everything else and is applied
# by EUSS — see app/api/requests.py.

bp = Blueprint("admin_api", __name__)


# ------------------------------------------------------------ employees (admin)
@bp.post("/api/employees")
@require_role("admin")
def create_employee():
    body = request.get_json(silent=True) or {}
    username = (body.get("username") or "").strip()
    password = body.get("password") or ""
    full_name = (body.get("full_name") or "").strip()
    email = (body.get("email") or "").strip() or None
    role = body.get("role") or "employee"
    department_id = body.get("department_id")
    if not username or not password or not full_name:
        return jsonify(error="username, password and full_name are required"), 400
    if role not in VALID_ROLES:
        return jsonify(error="Invalid role"), 400
    if db.query("SELECT 1 FROM employees WHERE username=%s", (username,), fetch="one"):
        return jsonify(error="Username already taken"), 409
    row = db.query(
        """INSERT INTO employees (username, password, full_name, email, role, department_id, is_admin)
           VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
        (username, password, full_name, email, role, department_id, role == "admin"), fetch="one")
    return jsonify(id=row["id"]), 201


@bp.put("/api/employees/<int:emp_id>")
@require_role("admin")
def update_employee(emp_id):
    body = request.get_json(silent=True) or {}
    if not db.query("SELECT 1 FROM employees WHERE id=%s", (emp_id,), fetch="one"):
        return jsonify(error="Employee not found"), 404
    fields, values = [], []
    for col in ("full_name", "password", "email", "department_id", "status"):
        if col in body:
            fields.append(f"{col}=%s")
            values.append(body[col])
    if "role" in body:
        if body["role"] not in VALID_ROLES:
            return jsonify(error="Invalid role"), 400
        fields.append("role=%s"); values.append(body["role"])
        fields.append("is_admin=%s"); values.append(body["role"] == "admin")
    if not fields:
        return jsonify(error="Nothing to update"), 400
    values.append(emp_id)
    db.query(f"UPDATE employees SET {', '.join(fields)} WHERE id=%s", tuple(values), fetch=None)
    return jsonify(ok=True)


@bp.delete("/api/employees/<int:emp_id>")
@require_role("admin")
def delete_employee(emp_id):
    if not db.query("SELECT 1 FROM employees WHERE id=%s", (emp_id,), fetch="one"):
        return jsonify(error="Employee not found"), 404
    db.query("DELETE FROM employees WHERE id=%s", (emp_id,), fetch=None)
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
    return jsonify(id=row["id"], name=name, access=[]), 201


@bp.put("/api/departments/<int:dept_id>/access")
@require_role("it_admin", "admin")
def set_department_access(dept_id):
    """IT Admin oversees which services a department grants (applied on the next
    onboard/shift into that department, not retroactively)."""
    service_ids = (request.get_json(silent=True) or {}).get("service_ids") or []
    if not db.query("SELECT 1 FROM departments WHERE id=%s", (dept_id,), fetch="one"):
        return jsonify(error="Department not found"), 404
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
    return jsonify(id=dept_id, access=[r["service_id"] for r in rows])


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
