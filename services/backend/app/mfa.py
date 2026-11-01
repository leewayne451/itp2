"""
Time-based one-time passwords (TOTP, RFC 6238) as the second factor.

Everything security-relevant about MFA lives here so the API blueprints stay thin:

  * secrets are encrypted at rest (Fernet) and only decrypted to check a code
  * a code is accepted for the current 30 s step +/- one step (clock drift), and
    each step can be used ONCE per account (replay protection, enforced atomically
    in SQL so two parallel requests cannot both succeed)
  * consecutive wrong codes lock the second factor for a few minutes
  * one-time recovery codes are stored hashed and consumed atomically
  * every outcome is written to the auth_events audit table
"""
import hashlib
import hmac
import re
import secrets
import time

import pyotp
from cryptography.fernet import Fernet, InvalidToken
from . import audit, db
from .config import (MFA_ENC_KEY, MFA_ISSUER, MFA_REQUIRED_ROLES,
                     MFA_MAX_FAILURES, MFA_LOCK_MINUTES, MFA_RECOVERY_CODES)

TOTP_DIGITS = 6
TOTP_INTERVAL = 30
TOTP_WINDOW = 1          # accept the previous / next step as well (clock drift)
_RECOVERY_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"   # no 0/O, 1/I/L confusion


# --------------------------------------------------------------------------- #
# Encryption of the shared secret at rest
# --------------------------------------------------------------------------- #
def _fernet():
    return Fernet(MFA_ENC_KEY.encode())


def encrypt_secret(secret):
    return _fernet().encrypt(secret.encode()).decode()


def decrypt_secret(token):
    try:
        return _fernet().decrypt(token.encode()).decode()
    except (InvalidToken, AttributeError):
        return None


# --------------------------------------------------------------------------- #
# Policy + audit
# --------------------------------------------------------------------------- #
def required_for(role):
    return role in MFA_REQUIRED_ROLES


def log_event(employee_id, username, event, detail=None):
    """Authentication events go to the tamper-evident audit trail (audit.py).
    The actor is the signed-in user when there is one (e.g. an administrator
    resetting someone's MFA), otherwise the person signing in."""
    from flask import g, has_request_context
    acting = has_request_context() and g.get("user")
    actor = None if acting else {"id": employee_id, "username": username, "role": _role_of(employee_id)}
    audit.record("auth", event, outcome=audit.outcome_for_auth(event), actor=actor,
                 target_type="employee", target_id=employee_id, target_label=username,
                 details={"detail": detail} if detail else None)


def _role_of(employee_id):
    if not employee_id:
        return None
    row = db.query("SELECT role FROM employees WHERE id=%s", (employee_id,), fetch="one")
    return row and row["role"]


def get_employee(emp_id):
    return db.query("SELECT * FROM employees WHERE id=%s", (emp_id,), fetch="one")


# --------------------------------------------------------------------------- #
# Brute-force lock
# --------------------------------------------------------------------------- #
def lock_remaining(emp):
    """Seconds the second factor stays locked for, or 0."""
    row = db.query("SELECT GREATEST(0, EXTRACT(EPOCH FROM (mfa_locked_until - now())))::int AS s "
                   "FROM employees WHERE id=%s", (emp["id"],), fetch="one")
    return (row and row["s"]) or 0


def _register_failure(emp):
    row = db.query(
        """UPDATE employees SET
               mfa_locked_until = CASE WHEN mfa_failed_count + 1 >= %s
                                       THEN now() + make_interval(mins => %s) ELSE mfa_locked_until END,
               mfa_failed_count = CASE WHEN mfa_failed_count + 1 >= %s THEN 0 ELSE mfa_failed_count + 1 END
           WHERE id=%s
           RETURNING mfa_failed_count, (mfa_locked_until > now()) AS locked""",
        (MFA_MAX_FAILURES, MFA_LOCK_MINUTES, MFA_MAX_FAILURES, emp["id"]), fetch="one")
    if row and row["locked"]:
        log_event(emp["id"], emp["username"], "mfa_locked",
                  f"{MFA_MAX_FAILURES} consecutive wrong codes; locked for {MFA_LOCK_MINUTES} min")


def _register_success(emp):
    db.query("UPDATE employees SET mfa_failed_count=0, mfa_locked_until=NULL WHERE id=%s", (emp["id"],), fetch=None)


# --------------------------------------------------------------------------- #
# TOTP
# --------------------------------------------------------------------------- #
def normalise(code):
    return re.sub(r"[\s\-]", "", code or "").upper()


def provisioning_uri(secret, username):
    return pyotp.TOTP(secret, digits=TOTP_DIGITS, interval=TOTP_INTERVAL).provisioning_uri(
        name=username, issuer_name=MFA_ISSUER)


def _matching_step(secret, code):
    """The time-step the code belongs to (within the drift window), or None."""
    if not re.fullmatch(r"\d{%d}" % TOTP_DIGITS, code):
        return None
    totp = pyotp.TOTP(secret, digits=TOTP_DIGITS, interval=TOTP_INTERVAL)
    now_step = int(time.time()) // TOTP_INTERVAL
    for step in range(now_step - TOTP_WINDOW, now_step + TOTP_WINDOW + 1):
        if hmac.compare_digest(totp.at(step * TOTP_INTERVAL), code):
            return step
    return None


def _claim_step(emp_id, step):
    """Atomically record the step as used. False if it (or a later one) was
    already used — i.e. the code is a replay."""
    row = db.query("UPDATE employees SET mfa_last_step=%s WHERE id=%s "
                   "AND (mfa_last_step IS NULL OR mfa_last_step < %s) RETURNING id",
                   (step, emp_id, step), fetch="one")
    return row is not None


# --------------------------------------------------------------------------- #
# Recovery codes
# --------------------------------------------------------------------------- #
def _hash_code(code):
    # Recovery codes are ~49 bits of randomness, so a fast hash is sufficient
    # (they cannot be brute-forced offline the way a human password can).
    return hashlib.sha256(("sj-recovery:" + normalise(code)).encode()).hexdigest()


def replace_recovery_codes(emp_id):
    """Invalidate any old codes and issue a fresh set. Returns them in plaintext —
    this is the only time they are ever visible."""
    codes = []
    for _ in range(MFA_RECOVERY_CODES):
        raw = "".join(secrets.choice(_RECOVERY_ALPHABET) for _ in range(10))
        codes.append(f"{raw[:5]}-{raw[5:]}")
    db.query("DELETE FROM mfa_recovery_codes WHERE employee_id=%s", (emp_id,), fetch=None)
    for c in codes:
        db.query("INSERT INTO mfa_recovery_codes (employee_id, code_hash) VALUES (%s,%s)",
                 (emp_id, _hash_code(c)), fetch=None)
    return codes


def recovery_codes_remaining(emp_id):
    return db.query("SELECT COUNT(*) AS c FROM mfa_recovery_codes WHERE employee_id=%s AND used_at IS NULL",
                    (emp_id,), fetch="one")["c"]


def _use_recovery_code(emp_id, code):
    row = db.query("UPDATE mfa_recovery_codes SET used_at=now() "
                   "WHERE employee_id=%s AND code_hash=%s AND used_at IS NULL RETURNING id",
                   (emp_id, _hash_code(code)), fetch="one")
    return row is not None


# --------------------------------------------------------------------------- #
# Verification (sign-in, and re-confirmation for sensitive MFA changes)
# --------------------------------------------------------------------------- #
def verify(emp, code, allow_recovery=True, context="sign-in"):
    """Check a second-factor code for an ENROLLED employee.

    Returns (ok, method, error) where method is 'totp' or 'recovery' and error is
    a user-facing message on failure.
    """
    remaining = lock_remaining(emp)
    if remaining:
        return False, None, f"Too many wrong codes. Try again in {max(1, (remaining + 59) // 60)} minute(s)."
    secret = decrypt_secret(emp.get("mfa_secret"))
    if not emp.get("mfa_enabled") or not secret:
        return False, None, "Two-factor authentication is not set up for this account."

    code = normalise(code)
    if not code:
        return False, None, "Enter the 6-digit code from your authenticator app."

    if code.isdigit():
        step = _matching_step(secret, code)
        if step is not None and _claim_step(emp["id"], step):
            _register_success(emp)
            log_event(emp["id"], emp["username"], "mfa_success", f"TOTP ({context})")
            return True, "totp", None
        reason = "code already used" if step is not None else "wrong code"
        _register_failure(emp)
        log_event(emp["id"], emp["username"], "mfa_failed", f"TOTP {reason} ({context})")
        return False, None, ("That code has already been used. Wait for the next one."
                             if step is not None else "Incorrect code.")

    if allow_recovery and _use_recovery_code(emp["id"], code):
        _register_success(emp)
        log_event(emp["id"], emp["username"], "recovery_code_used",
                  f"{recovery_codes_remaining(emp['id'])} remaining ({context})")
        return True, "recovery", None
    _register_failure(emp)
    log_event(emp["id"], emp["username"], "mfa_failed", f"invalid recovery code ({context})")
    return False, None, "Incorrect code."


# --------------------------------------------------------------------------- #
# Enrolment lifecycle
# --------------------------------------------------------------------------- #
def start_enrolment(emp):
    """Return (secret, otpauth_uri) for an employee who is not yet enrolled.
    Re-uses an unconfirmed pending secret so refreshing the page keeps the same QR."""
    secret = decrypt_secret(emp.get("mfa_pending_secret"))
    if not secret:
        secret = pyotp.random_base32()
        db.query("UPDATE employees SET mfa_pending_secret=%s WHERE id=%s",
                 (encrypt_secret(secret), emp["id"]), fetch=None)
        log_event(emp["id"], emp["username"], "mfa_enrolment_started")
    return secret, provisioning_uri(secret, emp["username"])


def confirm_enrolment(emp, code):
    """Activate TOTP once the user proves their app produces valid codes.
    Returns (recovery_codes, None) or (None, error)."""
    remaining = lock_remaining(emp)
    if remaining:
        return None, f"Too many wrong codes. Try again in {max(1, (remaining + 59) // 60)} minute(s)."
    secret = decrypt_secret(emp.get("mfa_pending_secret"))
    if not secret:
        return None, "Start the set-up again — no pending authenticator was found."
    step = _matching_step(secret, normalise(code))
    if step is None:
        _register_failure(emp)
        log_event(emp["id"], emp["username"], "mfa_failed", "wrong code during enrolment")
        return None, "Incorrect code. Check the time on your phone and try the current code."
    db.query("""UPDATE employees SET mfa_enabled=TRUE, mfa_secret=mfa_pending_secret, mfa_pending_secret=NULL,
                       mfa_last_step=%s, mfa_enrolled_at=now(), mfa_failed_count=0, mfa_locked_until=NULL
                WHERE id=%s""", (step, emp["id"]), fetch=None)
    codes = replace_recovery_codes(emp["id"])
    log_event(emp["id"], emp["username"], "mfa_enrolled", "TOTP authenticator activated")
    return codes, None


def disable(emp_id):
    """Remove TOTP entirely (user opt-out, or an administrator reset)."""
    db.query("""UPDATE employees SET mfa_enabled=FALSE, mfa_secret=NULL, mfa_pending_secret=NULL,
                       mfa_last_step=NULL, mfa_enrolled_at=NULL, mfa_failed_count=0, mfa_locked_until=NULL
                WHERE id=%s""", (emp_id,), fetch=None)
    db.query("DELETE FROM mfa_recovery_codes WHERE employee_id=%s", (emp_id,), fetch=None)
