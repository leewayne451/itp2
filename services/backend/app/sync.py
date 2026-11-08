"""
Keeps Keycloak (how people sign in) in step with the backend (who people are).

* push(emp_id) — called right after any change to an employee (create, edit,
  role change, deactivation, department-shift suspension). If Keycloak is down
  the change is not lost: the row stays `kc_sync_pending` and the background
  loop retries until it succeeds.
* First sync of an existing employee MIGRATES their credentials: the Argon2id
  password hash and the TOTP secret move into Keycloak (no password reset, no
  re-scanning a QR code) and are then erased from this database — afterwards
  Keycloak is the only place credentials live.
"""
import base64
import json
import threading
import time

from . import audit, db, keycloak
from .config import MFA_REQUIRED_ROLES, MFA_ENC_KEY, SYNC_INTERVAL_SECONDS

_lock = threading.RLock()
SYSTEM = {"id": None, "username": "system:keycloak-sync", "role": "system"}


def is_staff(emp):
    return emp["role"] in MFA_REQUIRED_ROLES


# --------------------------------------------------------------------------- #
# Credential migration helpers
# --------------------------------------------------------------------------- #
def _b64(s):
    return base64.b64decode(s + "=" * (-len(s) % 4))


def argon2_credential(phc):
    """argon2-cffi PHC string ($argon2id$v=19$m=..,t=..,p=..$salt$hash) ->
    Keycloak's stored-credential format (its default hasher is also Argon2id)."""
    if not phc or not phc.startswith("$argon2id$"):
        return None
    _, _, _, params, salt, digest = phc.split("$")
    p = dict(kv.split("=") for kv in params.split(","))
    raw = _b64(digest)
    return {
        "type": "password",
        "secretData": json.dumps({"value": base64.b64encode(raw).decode(),
                                  "salt": base64.b64encode(_b64(salt)).decode(), "additionalParameters": {}}),
        "credentialData": json.dumps({"hashIterations": int(p["t"]), "algorithm": "argon2", "additionalParameters": {
            "hashLength": [str(len(raw))], "memory": [p["m"]], "type": ["id"], "version": ["1.3"],
            "parallelism": [p["p"]]}}),
    }


def totp_credential(emp):
    """Existing Google-Authenticator enrolment -> Keycloak OTP credential (same secret,
    so the code in the user's app keeps working)."""
    if not (emp.get("mfa_enabled") and emp.get("mfa_secret") and MFA_ENC_KEY):
        return None
    from cryptography.fernet import Fernet, InvalidToken
    try:
        secret = Fernet(MFA_ENC_KEY.encode()).decrypt(emp["mfa_secret"].encode()).decode()
    except (InvalidToken, ValueError):
        return None
    return {"type": "otp", "userLabel": "Authenticator app (migrated)",
            "secretData": json.dumps({"value": secret}),
            "credentialData": json.dumps({"subType": "totp", "digits": 6, "counter": 0, "period": 30,
                                          "algorithm": "HmacSHA1", "secretEncoding": "BASE32"})}


# --------------------------------------------------------------------------- #
# Sync
# --------------------------------------------------------------------------- #
def push(emp_id, end_sessions=False):
    """Mark the employee for sync and try immediately. Returns True when Keycloak
    is up to date. `end_sessions` also signs the user out of every app."""
    db.query("UPDATE employees SET kc_sync_pending=TRUE WHERE id=%s", (emp_id,), fetch=None)
    if end_sessions:
        revoke_tokens(emp_id)
        request_logout(emp_id)
    try:
        sync_employee(emp_id, end_sessions=end_sessions)
        return True
    except keycloak.KeycloakError as e:
        print(f"[sync] employee {emp_id} queued for retry: {e.message}", flush=True)
        return False


def revoke_tokens(emp_id, at=None):
    """Access tokens (and service sessions) issued before now — or before `at`, a
    timezone-aware datetime — are refused by the API at once (they would otherwise
    stay valid for up to their 5-minute lifetime). Never moves the cut-off backwards."""
    db.query("""UPDATE employees SET tokens_revoked_after =
                    GREATEST(COALESCE(tokens_revoked_after, '-infinity'::timestamptz), COALESCE(%s, now()))
                WHERE id=%s""", (at, emp_id), fetch=None)


def request_logout(emp_id):
    """Remember that the user's Keycloak sessions must be ended. Cleared once
    Keycloak has done it, so a sign-out requested while Keycloak is down is not lost."""
    db.query("UPDATE employees SET kc_logout_pending=TRUE, kc_sync_pending=TRUE WHERE id=%s", (emp_id,), fetch=None)


def sync_employee(emp_id, end_sessions=False):
    with _lock:
        emp = db.query("SELECT * FROM employees WHERE id=%s", (emp_id,), fetch="one")
        if not emp or emp["role"] == "system":   # the HR-system account never signs in (CHANGE 008)
            return
        kc_id = emp["keycloak_id"]
        if kc_id and not keycloak.get_user(kc_id):
            # The Keycloak user vanished (e.g. only Keycloak's volume was reset).
            audit.record("admin", "keycloak_account_missing", actor=SYSTEM, target_type="employee",
                         target_id=emp["id"], target_label=emp["username"],
                         details={"action": "re-created without credentials; an administrator must reset the password"})
            kc_id = None
        if not kc_id:
            kc_id = _create_or_link(emp)
        else:
            keycloak.update_user(kc_id, emp)
        keycloak.set_staff(kc_id, is_staff(emp))
        if end_sessions or emp.get("kc_logout_pending") or emp["status"] != "active":
            keycloak.logout(kc_id)
        db.query("UPDATE employees SET keycloak_id=%s, kc_sync_pending=FALSE, kc_logout_pending=FALSE WHERE id=%s",
                 (kc_id, emp["id"]), fetch=None)
        return kc_id


def _create_or_link(emp):
    existing = keycloak.find_user(emp["username"])
    if existing:   # e.g. this database was reset but Keycloak's was not
        keycloak.update_user(existing["id"], emp)
        _erase_local_credentials(emp["id"])
        return existing["id"]
    creds, actions = [], []
    pw = argon2_credential(emp.get("password_hash"))
    otp = totp_credential(emp)
    if pw:
        creds.append(pw)
    if otp:
        creds.append(otp)
    if emp.get("must_change_password") or not pw:
        actions.append("UPDATE_PASSWORD")
    try:
        kc_id = keycloak.create_user(emp, creds, actions)
    except keycloak.KeycloakError as e:
        if e.status == 409:            # created concurrently: link to it
            return keycloak.find_user(emp["username"])["id"]
        raise
    _erase_local_credentials(emp["id"])
    audit.record("auth", "credentials_migrated_to_keycloak", actor=SYSTEM, target_type="employee",
                 target_id=emp["id"], target_label=emp["username"],
                 details={"password_hash": bool(pw), "totp": bool(otp),
                          "must_change_password": "UPDATE_PASSWORD" in actions})
    return kc_id


def _erase_local_credentials(emp_id):
    """Keycloak now holds the credentials — remove every copy from this database."""
    db.query("""UPDATE employees SET password_hash=NULL, must_change_password=FALSE, mfa_secret=NULL,
                       mfa_pending_secret=NULL, mfa_enabled=FALSE, mfa_last_step=NULL, mfa_enrolled_at=NULL
                WHERE id=%s""", (emp_id,), fetch=None)


def sync_pending():
    rows = db.query("SELECT id FROM employees WHERE (kc_sync_pending OR keycloak_id IS NULL) "
                    "AND role <> 'system' ORDER BY id")
    done = 0
    for r in rows:
        try:
            sync_employee(r["id"])
            done += 1
        except keycloak.KeycloakError as e:
            print(f"[sync] Keycloak not ready ({e.message}); will retry", flush=True)
            break
    return done


def delete(kc_id):
    if kc_id:
        keycloak.delete_user(kc_id)


# --------------------------------------------------------------------------- #
# Background loop: sync + pull Keycloak events into the audit trail
# --------------------------------------------------------------------------- #
def start_background():
    from . import kc_events, rotation

    def loop():
        while True:
            try:
                n = sync_pending()
                if n:
                    print(f"[sync] {n} employee(s) synchronised to Keycloak", flush=True)
                kc_events.ingest()
            except Exception as e:  # noqa: BLE001 — never let the loop die
                print(f"[sync] background error: {type(e).__name__}: {e}", flush=True)
            try:
                rotation.maybe_check()   # MJR: e-mail Directors about officers newly due (hourly)
            except Exception as e:  # noqa: BLE001
                print(f"[mjr] background error: {type(e).__name__}: {e}", flush=True)
            time.sleep(SYNC_INTERVAL_SECONDS)

    threading.Thread(target=loop, name="keycloak-sync", daemon=True).start()
