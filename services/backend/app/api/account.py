"""Self-service credentials: password policy, change, forgot/reset, and sessions."""
import hashlib
import secrets
import threading

from flask import Blueprint, request, jsonify, g

from .. import db, mfa, passwords, sessions
from ..auth import require_auth
from ..clients import client_ip
from ..config import CONSOLE_URL, RESET_TOKEN_MINUTES
from ..mailer import send_email
from ..ratelimit import rate_limited

bp = Blueprint("account_api", __name__)


def _body():
    return request.get_json(silent=True) or {}


# --------------------------------------------------------------------------- #
# Policy + change
# --------------------------------------------------------------------------- #
@bp.get("/api/password/policy")
def password_policy():
    return jsonify(passwords.policy())


@bp.post("/api/password/change")
@require_auth
@rate_limited("password-change", limit=10, window_seconds=300)
def change_password():
    """Signed-in change: needs the CURRENT password (so a hijacked session alone
    cannot take over the account). Ends every other session of the user."""
    emp = mfa.get_employee(g.user["id"])
    body = _body()
    ok, _ = passwords.verify_password(emp["password_hash"], body.get("current_password") or "")
    if not ok:
        if passwords.register_failure(emp["id"]):
            sessions.revoke_all(emp["id"], "account locked")
            mfa.log_event(emp["id"], emp["username"], "account_locked", "wrong current password (change)")
        mfa.log_event(emp["id"], emp["username"], "password_change_failed", "wrong current password")
        return jsonify(error="Your current password is incorrect."), 400
    new = body.get("new_password") or ""
    problem = passwords.validate_new_password(new, emp, current_hash=emp["password_hash"])
    if problem:
        return jsonify(error=problem), 400
    passwords.set_password(emp["id"], new)
    ended = sessions.revoke_all(emp["id"], "password changed", except_session_id=g.session["id"])
    mfa.log_event(emp["id"], emp["username"], "password_changed", f"self-service; {ended} other session(s) ended")
    return jsonify(ok=True, other_sessions_ended=ended)


# --------------------------------------------------------------------------- #
# Forgot password (emailed single-use link)
# --------------------------------------------------------------------------- #
GENERIC_FORGOT = ("If that account exists and has an email address, a reset link has been sent. "
                  f"It expires in {RESET_TOKEN_MINUTES} minutes.")


def _token_hash(token):
    return hashlib.sha256(token.encode()).hexdigest()


@bp.post("/api/password/forgot")
@rate_limited("password-forgot", limit=5, window_seconds=900)
def forgot_password():
    """Always answers the same way (no username enumeration). The e-mail is sent in
    the background so response time does not reveal whether one was sent."""
    username = (_body().get("username") or "").strip()
    emp = db.query("SELECT * FROM employees WHERE username=%s", (username,), fetch="one") if username else None
    if emp and emp["status"] == "active" and emp["email"]:
        token = secrets.token_urlsafe(32)
        # A new link invalidates any older unused one.
        db.query("UPDATE password_reset_tokens SET used_at=now() WHERE employee_id=%s AND used_at IS NULL",
                 (emp["id"],), fetch=None)
        db.query("""INSERT INTO password_reset_tokens (employee_id, token_hash, expires_at, requested_ip)
                    VALUES (%s,%s, now() + make_interval(mins => %s), %s)""",
                 (emp["id"], _token_hash(token), RESET_TOKEN_MINUTES, client_ip()), fetch=None)
        link = f"{CONSOLE_URL}/reset-password?token={token}"
        body = (f"Hello {emp['full_name']},\n\n"
                f"Someone asked to reset the ServiceJourney password for the account '{emp['username']}'.\n\n"
                f"To choose a new password, open this link within {RESET_TOKEN_MINUTES} minutes:\n{link}\n\n"
                f"The link works once. If you did not ask for this, ignore this e-mail — your password has "
                f"not been changed. Two-factor authentication (if enabled) is still required at sign-in.\n\n"
                f"Request made from: {client_ip() or 'unknown'}")
        threading.Thread(target=send_email, daemon=True,
                         args=(emp["email"], "[ServiceJourney] Reset your password", body)).start()
        mfa.log_event(emp["id"], emp["username"], "password_reset_requested", "reset link e-mailed")
    else:
        mfa.log_event(emp["id"] if emp else None, username[:64] or None, "password_reset_requested",
                      "no e-mail sent (unknown, inactive or no address)")
    return jsonify(ok=True, message=GENERIC_FORGOT)


def _valid_reset(token):
    if not token:
        return None
    return db.query("""SELECT t.id AS token_id, e.*
                       FROM password_reset_tokens t JOIN employees e ON e.id = t.employee_id
                       WHERE t.token_hash=%s AND t.used_at IS NULL AND t.expires_at > now()
                         AND e.status='active'""", (_token_hash(token),), fetch="one")


@bp.post("/api/password/reset/check")
@rate_limited("password-reset", limit=20, window_seconds=900)
def reset_check():
    row = _valid_reset(_body().get("token"))
    return jsonify(valid=bool(row), policy=passwords.policy())


@bp.post("/api/password/reset")
@rate_limited("password-reset", limit=20, window_seconds=900)
def reset_password():
    body = _body()
    row = _valid_reset(body.get("token"))
    if not row:
        return jsonify(error="This reset link is invalid or has expired. Request a new one."), 400
    new = body.get("new_password") or ""
    problem = passwords.validate_new_password(new, row, current_hash=row["password_hash"])
    if problem:
        return jsonify(error=problem), 400
    # Consume the token atomically — a link can only ever be used once.
    claimed = db.query("UPDATE password_reset_tokens SET used_at=now() WHERE id=%s AND used_at IS NULL RETURNING id",
                       (row["token_id"],), fetch="one")
    if not claimed:
        return jsonify(error="This reset link has already been used."), 400
    passwords.set_password(row["id"], new, must_change=False)   # also clears any lockout
    ended = sessions.revoke_all(row["id"], "password reset")
    mfa.log_event(row["id"], row["username"], "password_reset_completed", f"via e-mailed link; {ended} session(s) ended")
    return jsonify(ok=True)


# --------------------------------------------------------------------------- #
# Sessions
# --------------------------------------------------------------------------- #
@bp.get("/api/sessions")
@require_auth
def my_sessions():
    rows = sessions.list_active(g.user["id"])
    return jsonify([{
        "id": r["id"], "current": r["id"] == g.session["id"], "client": r["client"], "ip": r["ip"],
        "user_agent": r["user_agent"], "mfa": "otp" in r["amr"].split(","),
        "created_at": r["created_at"].isoformat(), "last_seen_at": r["last_seen_at"].isoformat(),
    } for r in rows])


@bp.post("/api/sessions/<int:sid>/revoke")
@require_auth
def revoke_session(sid):
    if not sessions.revoke(sid, "revoked by user", employee_id=g.user["id"]):
        return jsonify(error="Session not found"), 404
    mfa.log_event(g.user["id"], g.user["username"], "session_revoked", f"session #{sid}")
    return jsonify(ok=True)


@bp.post("/api/sessions/revoke-others")
@require_auth
def revoke_other_sessions():
    ended = sessions.revoke_all(g.user["id"], "signed out everywhere", except_session_id=g.session["id"])
    mfa.log_event(g.user["id"], g.user["username"], "sessions_revoked", f"signed out {ended} other session(s)")
    return jsonify(ok=True, ended=ended)
