"""
Pull Keycloak's login and admin events into the tamper-evident audit trail, so
sign-ins, failed sign-ins, lock-outs, password and TOTP changes — which now
happen inside Keycloak — stay in the one audit log the IT Admin reviews.

Each Keycloak event id is remembered (keycloak_ingested_events) so an event is
recorded exactly once, oldest first.

Admin changes made DIRECTLY in Keycloak (its admin console or API, bypassing the
ServiceJourney console) are also acted on here: disabling, deleting or signing out
a user, or resetting / removing their credentials, makes the backend refuse that
user's earlier access tokens and service sessions (tokens_revoked_after), within
one background cycle (SYNC_INTERVAL_SECONDS) — the same as a revocation done
through the console.
"""
import json
import threading
from datetime import datetime, timezone

from . import audit, db, keycloak

_lock = threading.Lock()

# Routine protocol traffic — not security-relevant on its own.
SKIP = {"CODE_TO_TOKEN", "REFRESH_TOKEN", "INTROSPECT_TOKEN", "CLIENT_LOGIN", "USER_INFO_REQUEST",
        "TOKEN_EXCHANGE", "PERMISSION_TOKEN", "CUSTOM_REQUIRED_ACTION"}
ACTIONS = {
    "LOGIN": "login_success", "LOGIN_ERROR": "login_failed", "LOGOUT": "logout", "LOGOUT_ERROR": "logout_failed",
    "UPDATE_PASSWORD": "password_changed", "UPDATE_PASSWORD_ERROR": "password_change_failed",
    "UPDATE_TOTP": "mfa_enrolled", "REMOVE_TOTP": "mfa_disabled",
    "SEND_RESET_PASSWORD": "password_reset_requested", "RESET_PASSWORD": "password_reset_completed",
    "RESET_PASSWORD_ERROR": "password_reset_failed", "CLIENT_LOGIN_ERROR": "client_authentication_failed",
    "USER_DISABLED_BY_TEMPORARY_LOCKOUT": "account_locked", "USER_DISABLED_BY_PERMANENT_LOCKOUT": "account_locked",
}
LOCKOUT_ERRORS = {"user_temporarily_disabled": "login_blocked"}


def _action(event):
    t = event["type"]
    err = event.get("error")
    if t == "LOGIN_ERROR" and err in LOCKOUT_ERRORS:
        return LOCKOUT_ERRORS[err]
    if t == "UPDATE_PASSWORD" and _follows_reset_email(event):
        return "password_reset_completed"
    if t in ("UPDATE_CREDENTIAL", "REMOVE_CREDENTIAL"):
        kind = (event.get("details") or {}).get("credential_type")
        if kind == "otp":
            return "mfa_enrolled" if t == "UPDATE_CREDENTIAL" else "mfa_disabled"
        if kind == "password":
            return None   # the matching UPDATE_PASSWORD event is recorded instead
    return ACTIONS.get(t, "keycloak_" + t.lower())


def _follows_reset_email(event):
    """Keycloak reports a password set through the "forgot password" e-mail link as
    UPDATE_PASSWORD; it shares its sign-in flow id (code_id) with the earlier
    SEND_RESET_PASSWORD event, which is how we tell it apart from a normal change."""
    flow = (event.get("details") or {}).get("code_id")
    if not flow:
        return False
    return db.query("SELECT 1 FROM audit_log WHERE action='password_reset_requested' AND payload LIKE %s LIMIT 1",
                    (f'%"auth_flow":"{flow}"%',), fetch="one") is not None


def _employee_by_kc(kc_id):
    if not kc_id:
        return None
    return db.query("SELECT id, username, role FROM employees WHERE keycloak_id=%s", (kc_id,), fetch="one")


def _seen(event_id):
    return db.query("SELECT 1 FROM keycloak_ingested_events WHERE id=%s", (event_id,), fetch="one") is not None


def _mark(event_id, kind):
    db.query("INSERT INTO keycloak_ingested_events (id, kind) VALUES (%s,%s) ON CONFLICT DO NOTHING",
             (event_id, kind), fetch=None)


def _ts(ms):
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc)


def _record_user_event(e):
    """True once handled (recorded, or deliberately skipped)."""
    if e["type"] in SKIP:
        return True
    action = _action(e)
    if not action:
        return True
    d = e.get("details") or {}
    emp = _employee_by_kc(e.get("userId"))
    username = (emp and emp["username"]) or d.get("username")
    failed = e["type"].endswith("_ERROR") or action in ("account_locked", "login_blocked")
    return audit.record("auth", action, outcome="failure" if failed else "success",
                 actor={"id": emp and emp["id"], "username": username, "role": emp and emp["role"]},
                 target_type="employee", target_id=emp and emp["id"], target_label=username,
                 ip=e.get("ipAddress"), client=e.get("clientId"), ts=_ts(e["time"]),
                 details={k: v for k, v in {
                     "source": "keycloak", "keycloak_event": e["type"], "error": e.get("error"),
                     "app": e.get("clientId"), "credential_type": d.get("credential_type"),
                     "auth_flow": d.get("code_id") if e["type"] == "SEND_RESET_PASSWORD" else None,
                     "keycloak_event_id": e["id"]}.items() if v}) is not None


def _revocation_reason(e):
    """Why this Keycloak admin event must end the user's existing sessions, or None."""
    if e.get("error") or e.get("resourceType") != "USER":
        return None
    parts = (e.get("resourcePath") or "").split("/")
    op = e.get("operationType")
    if len(parts) == 2 and op == "DELETE":
        return "deleted"
    if len(parts) == 2 and op == "UPDATE":
        try:
            rep = json.loads(e.get("representation") or "{}")
        except ValueError:
            rep = {}
        return "disabled" if rep.get("enabled") is False else None
    if len(parts) == 3 and op == "ACTION" and parts[2] in ("logout", "reset-password"):
        return parts[2]
    if len(parts) >= 3 and parts[2] == "credentials" and op in ("DELETE", "UPDATE"):
        return "credential_changed"
    return None


def _apply_revocation(e):
    """Refuse tokens/sessions issued before a revoking Keycloak admin event."""
    from . import sync       # sync imports this module lazily too
    path = e.get("resourcePath") or ""
    if not path.startswith("users/") or not _revocation_reason(e):
        return
    emp = _employee_by_kc(path.split("/")[1])
    if emp:
        sync.revoke_tokens(emp["id"], at=_ts(e["time"]))


def _record_admin_event(e):
    _apply_revocation(e)
    auth = e.get("authDetails") or {}
    actor_emp = _employee_by_kc(auth.get("userId"))
    d = e.get("details") or {}
    path = e.get("resourcePath") or ""
    target = _employee_by_kc(path.split("/")[1]) if path.startswith("users/") and "/" in path else None
    return audit.record("admin", f"keycloak_{e['operationType'].lower()}_{e['resourceType'].lower()}",
                 outcome="failure" if e.get("error") else "success",
                 actor={"id": actor_emp and actor_emp["id"],
                        "username": (actor_emp and actor_emp["username"]) or "service-account / Keycloak admin",
                        "role": actor_emp and actor_emp["role"]},
                 target_type="keycloak_" + e["resourceType"].lower(), target_id=path,
                 target_label=(target and target["username"]) or d.get("username") or path,
                 ip=auth.get("ipAddress"), client="keycloak-admin-api", ts=_ts(e["time"]),
                 details={"source": "keycloak", "operation": e["operationType"], "resource": e["resourceType"],
                          "path": path, "keycloak_event_id": e["id"]}) is not None


def ingest():
    """Record any Keycloak events not yet in the audit trail. Safe to call often."""
    if not _lock.acquire(blocking=False):
        return 0
    try:
        count = 0
        for kind, fetch, rec in (("user", keycloak.events, _record_user_event),
                                 ("admin", keycloak.admin_events, _record_admin_event)):
            new, first = [], 0
            while True:   # newest first; page back until we reach events already ingested
                batch = fetch(first, 200)
                fresh = [e for e in batch if not _seen(e["id"])]
                new.extend(fresh)
                if len(batch) < 200 or len(fresh) < len(batch):
                    break
                first += 200
            for e in sorted(new, key=lambda x: x["time"]):
                if not rec(e):      # audit write failed: leave it for the next run
                    return count
                _mark(e["id"], kind)
                count += 1
        return count
    except keycloak.KeycloakError:
        return 0
    finally:
        _lock.release()
