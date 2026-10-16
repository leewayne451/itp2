"""Login, identity, health, and the single access-check a protected service calls."""
from flask import Blueprint, request, jsonify, g

from .. import db
from ..auth import make_token, require_auth
from ..core import access_state

bp = Blueprint("auth_api", __name__)


@bp.get("/health")
def health():
    try:
        db.query("SELECT 1", fetch="one")
        return jsonify(status="ok")
    except Exception as e:  # noqa: BLE001
        return jsonify(status="error", detail=str(e)), 500


@bp.post("/api/login")
def login():
    body = request.get_json(silent=True) or {}
    username = (body.get("username") or "").strip()
    password = body.get("password") or ""
    row = db.query("SELECT * FROM employees WHERE username = %s AND status = 'active'", (username,), fetch="one")
    if not row or row["password"] != password:
        return jsonify(error="Invalid credentials"), 401
    return jsonify(
        token=make_token(row["id"], row["role"]),
        user={"id": row["id"], "username": row["username"], "full_name": row["full_name"], "role": row["role"]},
    )


@bp.get("/api/me")
@require_auth
def me():
    u = g.user
    return jsonify(id=u["id"], username=u["username"], full_name=u["full_name"], role=u["role"])


@bp.post("/api/service/access")
def service_access():
    """The single integration point for a protected service.

    A production service authenticates its own users. It calls this endpoint only
    to ask whether an already-identified user currently has access to it, so NO
    password is checked here — establishing identity is the caller's job.

    Body: {"service": "<key>", "username": "<name>"}  (or "employee_id": <id>).
    Always returns HTTP 200 with an explicit authorization decision, so the caller
    can tell a definite 'no' apart from a failure to get an answer (a non-200).
      {"authorized": true,  "reason": null, "employee": {...}, "service": {...}}
      {"authorized": false, "reason": "<why>"}
    Reasons: unknown_service, offboarded, inactive, suspended, revoked.
    """
    body = request.get_json(silent=True) or {}
    service_key = (body.get("service") or "").strip()
    username = (body.get("username") or "").strip()
    emp_id = body.get("employee_id")

    service = db.query("SELECT id, key, name FROM services WHERE key = %s", (service_key,), fetch="one")
    if not service:
        return jsonify(authorized=False, reason="unknown_service")

    if username:
        emp = db.query("SELECT id, username, full_name, status FROM employees WHERE username = %s",
                       (username,), fetch="one")
    elif emp_id is not None:
        emp = db.query("SELECT id, username, full_name, status FROM employees WHERE id = %s",
                       (emp_id,), fetch="one")
    else:
        return jsonify(error="username or employee_id is required"), 400

    if not emp:
        return jsonify(authorized=False, reason="offboarded")
    if emp["status"] != "active":
        # 'suspended' is the employee-level hold applied during a department shift.
        return jsonify(authorized=False, reason=("suspended" if emp["status"] == "suspended" else "inactive"))
    blocked = access_state(emp["id"], service["id"])
    if blocked:
        return jsonify(authorized=False, reason=blocked)
    return jsonify(authorized=True, reason=None,
                   employee={"id": emp["id"], "username": emp["username"], "full_name": emp["full_name"]},
                   service={"key": service["key"], "name": service["name"]})
