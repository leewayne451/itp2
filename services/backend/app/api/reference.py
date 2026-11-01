"""Read-only reference data available to any authenticated console user."""
import time

from flask import Blueprint, jsonify, g

from .. import db, keycloak
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
        SELECT e.id, e.username, e.full_name, e.email, e.role, e.status, e.department_id, d.name AS department_name,
               e.keycloak_id, to_char(e.department_since, 'YYYY-MM-DD') AS department_since
        FROM employees e LEFT JOIN departments d ON d.id = e.department_id
        ORDER BY e.full_name
    """)
    for r in rows:
        acc = db.query("SELECT service_id FROM employee_access WHERE employee_id=%s", (r["id"],))
        r["access"] = [a["service_id"] for a in acc]
        kc_id = r.pop("keycloak_id", None)
        if g.user["role"] == "admin":   # two-factor status (from Keycloak) is only shown to administrators
            r["mfa_enabled"] = _has_otp(kc_id)
    return jsonify(rows)


_otp_cache = {}


def _has_otp(kc_id):
    """Whether the Keycloak account has an authenticator (cached 20 s; None if unknown)."""
    if not kc_id:
        return None
    hit = _otp_cache.get(kc_id)
    if hit and hit[1] > time.time():
        return hit[0]
    try:
        value = keycloak.has_otp(kc_id)
    except keycloak.KeycloakError:
        return None
    _otp_cache[kc_id] = (value, time.time() + 20)
    return value
