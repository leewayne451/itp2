"""Read-only reference data available to any authenticated console user."""
from flask import Blueprint, jsonify

from .. import db
from ..auth import require_auth

bp = Blueprint("reference_api", __name__)


@bp.get("/api/services")
@require_auth
def list_services():
    return jsonify(db.query("SELECT id, key, name, resource_type FROM services ORDER BY id"))


@bp.get("/api/departments")
@require_auth
def list_departments():
    depts = db.query("SELECT id, name FROM departments ORDER BY name")
    for d in depts:
        rows = db.query("SELECT service_id FROM department_access WHERE department_id=%s", (d["id"],))
        d["access"] = [r["service_id"] for r in rows]
    return jsonify(depts)


@bp.get("/api/employees")
@require_auth
def list_employees():
    rows = db.query("""
        SELECT e.id, e.username, e.full_name, e.email, e.role, e.status, e.department_id, d.name AS department_name
        FROM employees e LEFT JOIN departments d ON d.id = e.department_id
        ORDER BY e.full_name
    """)
    for r in rows:
        acc = db.query("SELECT service_id FROM employee_access WHERE employee_id=%s", (r["id"],))
        r["access"] = [a["service_id"] for a in acc]
    return jsonify(rows)
