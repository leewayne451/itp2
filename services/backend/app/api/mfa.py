"""Self-service TOTP management for a signed-in user, plus the admin reset and the
authentication audit log."""
from flask import Blueprint, request, jsonify, g

from .. import audit, mfa, sessions
from ..auth import require_auth, require_role

bp = Blueprint("mfa_api", __name__)


def _code():
    return (request.get_json(silent=True) or {}).get("code", "")


@bp.get("/api/mfa/status")
@require_auth
def status():
    emp = mfa.get_employee(g.user["id"])
    return jsonify(
        enabled=emp["mfa_enabled"],
        required=mfa.required_for(emp["role"]),
        pending=bool(emp["mfa_pending_secret"]) and not emp["mfa_enabled"],
        enrolled_at=emp["mfa_enrolled_at"].isoformat() if emp["mfa_enrolled_at"] else None,
        recovery_codes_remaining=mfa.recovery_codes_remaining(emp["id"]) if emp["mfa_enabled"] else 0,
    )


@bp.post("/api/mfa/setup")
@require_auth
def setup():
    """Opt-in enrolment for a signed-in user (e.g. an ordinary employee)."""
    emp = mfa.get_employee(g.user["id"])
    if emp["mfa_enabled"]:
        return jsonify(error="Two-factor authentication is already on."), 409
    secret, uri = mfa.start_enrolment(emp)
    return jsonify(secret=secret, otpauth_uri=uri, issuer=mfa.MFA_ISSUER, username=emp["username"])


@bp.post("/api/mfa/confirm")
@require_auth
def confirm():
    emp = mfa.get_employee(g.user["id"])
    if emp["mfa_enabled"]:
        return jsonify(error="Two-factor authentication is already on."), 409
    codes, error = mfa.confirm_enrolment(emp, _code())
    if error:
        return jsonify(error=error), 400
    # The user just proved possession of the authenticator: mark THIS session as
    # MFA-verified and end their other (password-only) sessions, which are no
    # longer acceptable for an account with MFA on.
    sessions.add_method(g.session["id"], "otp")
    sessions.revoke_all(emp["id"], "MFA enabled", except_session_id=g.session["id"])
    return jsonify(recovery_codes=codes)


@bp.post("/api/mfa/recovery-codes")
@require_auth
def regenerate_recovery_codes():
    """Issue a fresh set of recovery codes (old ones stop working). Needs a
    current TOTP code — a recovery code cannot be used to mint more of them."""
    emp = mfa.get_employee(g.user["id"])
    ok, _, error = mfa.verify(emp, _code(), allow_recovery=False, context="regenerate recovery codes")
    if not ok:
        return jsonify(error=error), 400
    codes = mfa.replace_recovery_codes(emp["id"])
    mfa.log_event(emp["id"], emp["username"], "recovery_codes_regenerated")
    return jsonify(recovery_codes=codes)


@bp.post("/api/mfa/disable")
@require_auth
def disable():
    """Turn TOTP off — only where the role does not require it, and only with a
    current code, so a hijacked session alone cannot strip the second factor."""
    emp = mfa.get_employee(g.user["id"])
    if mfa.required_for(emp["role"]):
        return jsonify(error="Your role requires two-factor authentication; it cannot be turned off."), 403
    ok, _, error = mfa.verify(emp, _code(), allow_recovery=False, context="disable MFA")
    if not ok:
        return jsonify(error=error), 400
    mfa.disable(emp["id"])
    mfa.log_event(emp["id"], emp["username"], "mfa_disabled", "turned off by the user")
    return jsonify(ok=True)


@bp.post("/api/employees/<int:emp_id>/mfa/reset")
@require_role("admin")
def admin_reset(emp_id):
    """Lost phone: an administrator clears the authenticator. The user must enrol
    again at their next sign-in (if their role requires it)."""
    emp = mfa.get_employee(emp_id)
    if not emp:
        return jsonify(error="Employee not found"), 404
    mfa.disable(emp_id)
    ended = sessions.revoke_all(emp_id, "MFA reset by administrator")
    audit.record("admin", "mfa_reset", target_type="employee", target_id=emp_id, target_label=emp["username"],
                 details={"sessions_ended": ended})
    return jsonify(ok=True)
