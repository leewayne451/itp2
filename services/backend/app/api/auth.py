"""Health, the signed-in user's identity, and the single access-check a protected
service calls. Sign-in itself happens in Keycloak (single sign-on)."""
from flask import Blueprint, request, jsonify, g

from .. import audit, db
from ..auth import require_auth
from ..clients import require_client
from ..config import MFA_REQUIRED_ROLES
from ..core import access_state

bp = Blueprint("auth_api", __name__)


@bp.get("/health")
def health():
    try:
        db.query("SELECT 1", fetch="one")
        return jsonify(status="ok")
    except Exception:  # noqa: BLE001 — never leak internals from an unauthenticated endpoint
        return jsonify(status="error"), 500


@bp.get("/api/me")
@require_auth
def me():
    """Who the bearer token belongs to (the console calls this right after SSO)."""
    u, c = g.user, g.claims
    return jsonify(id=u["id"], username=u["username"], full_name=u["full_name"], role=u["role"],
                   mfa=("otp" in (c.get("amr") or [])), mfa_required=u["role"] in MFA_REQUIRED_ROLES,
                   auth_time=c.get("auth_time"), session_id=c.get("sid"))


_EMP_SQL = """SELECT id, username, full_name, status, EXTRACT(EPOCH FROM tokens_revoked_after) AS revoked_after
              FROM employees WHERE """


@bp.post("/api/service/access")
@require_client()
def service_access():
    """The single integration point for a protected service.

    A production service authenticates its own users. It calls this endpoint only
    to ask whether an already-identified user currently has access to it, so NO
    password is checked here — establishing identity is the caller's job.

    Only an authenticated first-party client (X-Client-Id / X-Client-Secret) may
    ask, and only about ITSELF — service1 cannot probe access to service2, and an
    anonymous caller cannot enumerate usernames.

    Body: {"service": "<key>", "username": "<name>"}  (or "employee_id": <id>),
          optionally "session_issued_at": <unix time the service's sign-in was issued>
          — a session older than the employee's last forced sign-out / reset is refused.
    Always returns HTTP 200 with an explicit authorization decision, so the caller
    can tell a definite 'no' apart from a failure to get an answer (a non-200).
      {"authorized": true,  "reason": null, "employee": {...}, "service": {...}}
      {"authorized": false, "reason": "<why>"}
    Reasons: unknown_service, offboarded, inactive, suspended, revoked, signed_out.
    """
    body = request.get_json(silent=True) or {}
    service_key = (body.get("service") or "").strip()
    username = (body.get("username") or "").strip()
    emp_id = body.get("employee_id")

    if service_key != g.client:
        return jsonify(error="A client may only check access to its own service"), 403
    service = db.query("SELECT id, key, name FROM services WHERE key = %s", (service_key,), fetch="one")
    if not service:
        return jsonify(authorized=False, reason="unknown_service")

    if username:
        emp = db.query(_EMP_SQL + "username = %s", (username,), fetch="one")
    elif emp_id is not None:
        emp = db.query(_EMP_SQL + "id = %s", (emp_id,), fetch="one")
    else:
        return jsonify(error="username or employee_id is required"), 400

    context = body.get("context") or "check"   # 'login' or 'session' (the 5-second live re-check)

    def decide(authorized, reason, emp_row=None):
        # Every DENIAL is audited; grants are audited at sign-in (not on each 5-second
        # re-check of a live session, which would only add noise).
        if not authorized or context == "login":
            audit.record("access", "service_access_check", outcome="success" if authorized else "denied",
                         actor={"id": None, "username": f"service:{g.client}", "role": "service"},
                         target_type="employee", target_id=emp_row and emp_row["id"],
                         target_label=(emp_row and emp_row["username"]) or username or None,
                         details={"service": service_key, "context": context, "reason": reason})
        return jsonify(authorized=authorized, reason=reason) if not authorized else None

    if not emp:
        return decide(False, "offboarded")
    if emp["status"] != "active":
        # 'suspended' is the employee-level hold applied during a department shift.
        return decide(False, "suspended" if emp["status"] == "suspended" else "inactive", emp)
    issued_at = body.get("session_issued_at")
    if isinstance(issued_at, (int, float)) and emp["revoked_after"] and issued_at <= int(emp["revoked_after"]):
        return decide(False, "signed_out", emp)
    blocked = access_state(emp["id"], service["id"])
    if blocked:
        return decide(False, blocked, emp)
    decide(True, None, emp)
    return jsonify(authorized=True, reason=None,
                   employee={"id": emp["id"], "username": emp["username"], "full_name": emp["full_name"]},
                   service={"key": service["key"], "name": service["name"]})
