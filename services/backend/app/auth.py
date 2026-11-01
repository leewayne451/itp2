"""Authentication of API calls (Keycloak access tokens) and role checks."""
import functools

from flask import request, jsonify, g

from . import audit, db, tokens
from .config import MFA_REQUIRED_ROLES


def require_auth(fn):
    """The caller must present a valid Keycloak access token for an ACTIVE employee.

    On top of the token's own checks (tokens.validate):
      * the token's subject must be the Keycloak account linked to the employee;
      * tokens issued before the employee's `tokens_revoked_after` are refused —
        so deactivation, role changes, password/MFA resets and forced sign-outs
        take effect immediately, not when the token would have expired;
      * staff roles must have signed in WITH their second factor (amr has 'otp').
    """
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        auth = request.headers.get("Authorization", "")
        if not auth.startswith("Bearer "):
            return jsonify(error="Missing bearer token"), 401
        try:
            claims = tokens.validate(auth[7:])
        except tokens.InvalidToken as e:
            return jsonify(error=str(e)), 401
        emp = db.query("""SELECT id, username, full_name, email, role, status,
                                 EXTRACT(EPOCH FROM tokens_revoked_after) AS revoked_after
                          FROM employees WHERE keycloak_id = %s""", (claims["sub"],), fetch="one")
        if not emp:
            return jsonify(error="No employee is linked to this account"), 401
        if emp["status"] != "active":
            return jsonify(error="Account not active"), 401
        if emp["revoked_after"] and claims["iat"] <= int(emp["revoked_after"]):  # iat has 1 s resolution
            return jsonify(error="Session ended"), 401
        if emp["role"] in MFA_REQUIRED_ROLES and "otp" not in (claims.get("amr") or []):
            return jsonify(error="Two-factor authentication required", mfa_required=True), 401
        g.user = emp
        g.claims = claims
        return fn(*args, **kwargs)

    return wrapper


def require_role(*roles):
    def deco(fn):
        @functools.wraps(fn)
        @require_auth
        def wrapper(*args, **kwargs):
            if g.user["role"] not in roles:
                audit.record("access", "permission_denied", outcome="denied", target_type="endpoint",
                             target_id=request.path, target_label=f"{request.method} {request.path}",
                             details={"required_roles": list(roles)})
                return jsonify(error=f"Requires role: {', '.join(roles)}"), 403
            return fn(*args, **kwargs)
        return wrapper
    return deco
