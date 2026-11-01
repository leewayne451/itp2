"""
Thin client for the Keycloak Admin REST API, used by the backend's own service
account (client `servicejourney-backend`, roles: manage-users, view-users,
query-users, query-groups, view-events — nothing realm-wide).

The backend stays the HR source of truth for WHO exists and in WHICH role;
Keycloak is the source of truth for HOW they authenticate (password, TOTP,
lock-out, sessions). This module pushes the former into the latter.
"""
import threading
import time

import requests

from .config import (KEYCLOAK_URL, KEYCLOAK_REALM, KEYCLOAK_CLIENT_ID, KEYCLOAK_CLIENT_SECRET,
                     STAFF_GROUP, verify)

ADMIN = f"{KEYCLOAK_URL}/admin/realms/{KEYCLOAK_REALM}"
TOKEN_URL = f"{KEYCLOAK_URL}/realms/{KEYCLOAK_REALM}/protocol/openid-connect/token"
TIMEOUT = 10

_http = requests.Session()
_token = {"value": None, "exp": 0}
_lock = threading.Lock()
_group_id = {}


class KeycloakError(Exception):
    """Keycloak refused or could not be reached. `message` is safe to show."""

    def __init__(self, message, status=None):
        super().__init__(message)
        self.message = message
        self.status = status


def _access_token():
    with _lock:
        if _token["value"] and _token["exp"] > time.time() + 30:
            return _token["value"]
        try:
            r = _http.post(TOKEN_URL, data={"grant_type": "client_credentials", "client_id": KEYCLOAK_CLIENT_ID,
                                            "client_secret": KEYCLOAK_CLIENT_SECRET},
                           timeout=TIMEOUT, verify=verify())
        except requests.RequestException as e:
            raise KeycloakError("Keycloak is not reachable") from e
        if r.status_code != 200:
            raise KeycloakError(f"Keycloak refused the backend's client credentials ({r.status_code})", r.status_code)
        data = r.json()
        _token.update(value=data["access_token"], exp=time.time() + int(data.get("expires_in", 60)))
        return _token["value"]


def _call(method, path, **kw):
    try:
        r = _http.request(method, ADMIN + path, headers={"Authorization": f"Bearer {_access_token()}"},
                          timeout=TIMEOUT, verify=verify(), **kw)
    except requests.RequestException as e:
        raise KeycloakError("Keycloak is not reachable") from e
    if r.status_code >= 400:
        try:
            body = r.json()
            msg = body.get("error_description") or body.get("errorMessage") or body.get("error") or r.text
        except ValueError:
            msg = r.text
        raise KeycloakError(_friendly(msg), r.status_code)
    return r


def _friendly(msg):
    """Keycloak's password-policy errors -> plain English for the admin UI."""
    table = {
        "invalidPasswordMinLengthMessage": "Password must be at least 12 characters.",
        "invalidPasswordMaxLengthMessage": "Password must be at most 128 characters.",
        "invalidPasswordNotUsernameMessage": "Password must not be the username.",
        "invalidPasswordNotContainsUsernameMessage": "Password must not contain the username.",
        "invalidPasswordNotEmailMessage": "Password must not be the email address.",
        "invalidPasswordBlacklistedMessage": "That password is too common.",
        "invalidPasswordHistoryMessage": "That password was used recently.",
    }
    for key, text in table.items():
        if key in str(msg):
            return f"Password rejected by the policy: {text}"
    return str(msg)[:300]


# --------------------------------------------------------------------------- #
# Users
# --------------------------------------------------------------------------- #
def find_user(username):
    users = _call("GET", "/users", params={"username": username, "exact": "true"}).json()
    return users[0] if users else None


def get_user(kc_id):
    try:
        return _call("GET", f"/users/{kc_id}").json()
    except KeycloakError as e:
        if e.status == 404:
            return None
        raise


def _names(full_name):
    parts = (full_name or "").split(" ", 1)
    return parts[0] or "-", (parts[1] if len(parts) > 1 else "-")


def user_representation(emp):
    first, last = _names(emp["full_name"])
    # Keycloak's default user profile requires first name, last name and email; without
    # them the user would be stopped by an "update profile" page at sign-in.
    return {"username": emp["username"], "firstName": first, "lastName": last,
            "email": emp.get("email") or f"{emp['username']}@servicejourney.local", "emailVerified": True,
            "enabled": emp["status"] == "active"}


def create_user(emp, credentials=(), required_actions=()):
    rep = dict(user_representation(emp), credentials=list(credentials), requiredActions=list(required_actions))
    r = _call("POST", "/users", json=rep)
    return r.headers["Location"].rstrip("/").rsplit("/", 1)[1]


def update_user(kc_id, emp):
    current = _call("GET", f"/users/{kc_id}").json()
    current.update(user_representation(emp))
    _call("PUT", f"/users/{kc_id}", json=current)


def delete_user(kc_id):
    try:
        _call("DELETE", f"/users/{kc_id}")
    except KeycloakError as e:
        if e.status != 404:
            raise


def logout(kc_id):
    """End every Keycloak session of the user (all apps, via back-channel logout)."""
    _call("POST", f"/users/{kc_id}/logout")


def set_temporary_password(kc_id, password):
    """Keycloak checks the realm password policy and forces a change at next sign-in."""
    _call("PUT", f"/users/{kc_id}/reset-password", json={"type": "password", "value": password, "temporary": True})


def clear_lockout(kc_id):
    """Lift a brute-force lock-out (e.g. after a help-desk password reset)."""
    try:
        _call("DELETE", f"/attack-detection/brute-force/users/{kc_id}")
    except KeycloakError as e:
        if e.status != 404:
            raise


def credentials(kc_id):
    return _call("GET", f"/users/{kc_id}/credentials").json()


def has_otp(kc_id):
    return any(c.get("type") == "otp" for c in credentials(kc_id))


def remove_otp(kc_id):
    removed = 0
    for c in credentials(kc_id):
        if c.get("type") == "otp":
            _call("DELETE", f"/users/{kc_id}/credentials/{c['id']}")
            removed += 1
    return removed


def _staff_group_id():
    if STAFF_GROUP not in _group_id:
        groups = _call("GET", "/groups", params={"search": STAFF_GROUP, "exact": "true"}).json()
        match = [g for g in groups if g["name"] == STAFF_GROUP]
        if not match:
            raise KeycloakError(f"Keycloak group '{STAFF_GROUP}' is missing from the realm")
        _group_id[STAFF_GROUP] = match[0]["id"]
    return _group_id[STAFF_GROUP]


def set_staff(kc_id, is_staff):
    """Staff (roles that require MFA) are members of the 'staff' group, whose login
    flow demands TOTP and makes them set it up on first sign-in."""
    gid = _staff_group_id()
    member = any(g["id"] == gid for g in _call("GET", f"/users/{kc_id}/groups").json())
    if is_staff and not member:
        _call("PUT", f"/users/{kc_id}/groups/{gid}")
    elif member and not is_staff:
        _call("DELETE", f"/users/{kc_id}/groups/{gid}")


# --------------------------------------------------------------------------- #
# Events (pulled into the audit trail)
# --------------------------------------------------------------------------- #
def events(first=0, max_results=200):
    return _call("GET", "/events", params={"first": first, "max": max_results}).json()


def admin_events(first=0, max_results=200):
    return _call("GET", "/admin-events", params={"first": first, "max": max_results}).json()
