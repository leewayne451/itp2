"""
Service1 — a demo protected service.

Users sign in through ServiceJourney SINGLE SIGN-ON (Keycloak, OpenID Connect
authorization code + PKCE): this service never sees a password or TOTP code. With
the verified identity it asks the platform one question — via /api/service/access
— whether that user currently has access to THIS service, and keeps re-asking
while they are signed in. Signing out here signs out of every ServiceJourney app,
and a sign-out elsewhere ends this session too (OIDC back-channel logout).

Session limits (kept in line with the Keycloak realm):
  * idle 30 min — only real page views count, not the 5-second status poll;
  * absolute 8 h from sign-in, whatever the activity;
  * every minute the session's refresh token is presented to Keycloak, so an
    account disabled or signed out directly in Keycloak, or an expired SSO
    session, ends this session too. If Keycloak is merely UNREACHABLE the session
    carries on (fail open), so an outage does not sign everyone out;
  * the backend also refuses sessions that started before the employee's last
    forced sign-out / password or 2FA reset (works even while Keycloak is down).
Session state lives on the server (the cookie holds a random handle only), so a
restart of this service signs users out here — the SSO session signs them back in.
"""
import os
import secrets
import threading
import time
from datetime import timedelta

import requests
from flask import Flask, render_template, request, redirect, url_for, session, jsonify

from oidc import OIDCClient, OIDCError

app = Flask(__name__)


def _required_secret(name):
    """No hard-coded fallback: refuse to start without a strong secret from .env."""
    value = os.environ.get(name, "")
    if len(value) < 32 or "change-me" in value:
        raise RuntimeError(f"{name} is missing or too weak. Run `python scripts/generate_secrets.py`, "
                           f"then `docker compose up -d --force-recreate`.")
    return value


app.secret_key = _required_secret("SECRET_KEY")
# This service's client credential towards the backend (service-to-service auth):
# required to call /api/service/access.
CLIENT_ID = os.environ.get("CLIENT_ID", "service1")
CLIENT_SECRET = _required_secret("CLIENT_SECRET")
# Unique cookie name so this service's session does not collide with the other
# apps on localhost (cookies are scoped by host only, not by port).
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_NAME"] = os.environ.get("SESSION_COOKIE_NAME", "svc1_session")
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(hours=8)     # cookie only; idle / max limits are server-side

BACKEND_URL = os.environ.get("BACKEND_URL", "https://backend:5000")
# Single sign-on: this service's OpenID Connect client in Keycloak.
sso = OIDCClient(os.environ.get("OIDC_CLIENT_ID", "service1"), _required_secret("OIDC_CLIENT_SECRET"),
                 os.environ.get("APP_URL", "https://localhost:8081"))
SERVICE_KEY = os.environ.get("SERVICE_KEY", "service1")
SERVICE_NAME = os.environ.get("SERVICE_NAME", "Service One")
TIMEOUT = 10

# --------------------------------------------------------------------------- #
# TLS — serve HTTPS and verify the backend against our own CA.
# --------------------------------------------------------------------------- #
TLS_CERT = os.environ.get("TLS_CERT", "/certs/server.crt")
TLS_KEY = os.environ.get("TLS_KEY", "/certs/server.key")
CA_BUNDLE = os.environ.get("CA_BUNDLE", "/certs/ca.crt")


def tls_enabled():
    return os.path.exists(TLS_CERT) and os.path.exists(TLS_KEY)


def ssl_context():
    if tls_enabled():
        return (TLS_CERT, TLS_KEY)
    print("[tls] certificate not found — serving plain HTTP", flush=True)
    return None


def verify():
    return CA_BUNDLE if os.path.exists(CA_BUNDLE) else True


# Shown on the login page after a live session is terminated.
REVOKED_MESSAGES = {
    "revoked": "Your access to this service has been revoked by an administrator.",
    "inactive": "Your account has been deactivated.",
    "suspended": "Your access is temporarily suspended (department transfer in progress).",
    "offboarded": "Your account no longer exists.",
    "unknown_service": "This service is no longer available.",
    "expired": "Your session has ended. Please sign in again.",
    "signed_out": "You have been signed out of ServiceJourney (single sign-on).",
}


def access_check(identity, context="session"):
    """Ask the backend (source of truth) whether this identity may use this service.

    `identity` is {"username": ...} or {"employee_id": ...}. Returns
    (authorized, reason, employee): authorized is True/False when the backend
    answered, or None when it could not be reached — letting each caller choose
    to fail open or closed.
    """
    try:
        resp = requests.post(
            f"{BACKEND_URL}/api/service/access",
            json={"service": SERVICE_KEY, "context": context, **identity},
            headers={"X-Client-Id": CLIENT_ID, "X-Client-Secret": CLIENT_SECRET},
            timeout=TIMEOUT, verify=verify(),
        )
        if resp.status_code != 200:      # e.g. our client credential was rejected: no answer
            return None, "unreachable", None
        data = resp.json()
        return bool(data.get("authorized")), data.get("reason"), data.get("employee")
    except (requests.RequestException, ValueError):
        return None, "unreachable", None


def still_allowed(emp_id, issued_at=None):
    """Live-session re-check. Fails OPEN on a backend blip so a transient error
    doesn't sign everyone out."""
    identity = {"employee_id": emp_id}
    if issued_at:
        identity["session_issued_at"] = issued_at
    ok, reason, _ = access_check(identity)
    if ok is None:
        return True, None
    return ok, reason


# --------------------------------------------------------------------------- #
# Single sign-on (Keycloak) — server-side session store. The cookie holds only a
# random handle; the refresh / ID tokens stay here. Entries are indexed by
# Keycloak session id so a back-channel logout ends the matching sessions.
# --------------------------------------------------------------------------- #
IDLE_TIMEOUT = int(os.environ.get("IDLE_TIMEOUT_MINUTES", 30)) * 60       # realm ssoSessionIdleTimeout
MAX_SESSION = int(os.environ.get("MAX_SESSION_HOURS", 8)) * 3600           # realm ssoSessionMaxLifespan
KC_CHECK_SECONDS = int(os.environ.get("KC_CHECK_SECONDS", 60))

_lock = threading.Lock()
_sessions = {}      # handle -> {"refresh", "id_token", "kc_sid", "issued_at", "login_at", "last_active", "kc_checked"}
_by_kc_sid = {}     # Keycloak session id -> {handles}


def _store_put(tokens, claims):
    handle = secrets.token_urlsafe(32)
    now = time.time()
    with _lock:
        _prune(now)
        _sessions[handle] = {"refresh": tokens.get("refresh_token"), "id_token": tokens.get("id_token"),
                             "kc_sid": claims.get("sid"), "issued_at": int(claims.get("iat") or now),
                             "login_at": now, "last_active": now, "kc_checked": now}
        _by_kc_sid.setdefault(claims.get("sid"), set()).add(handle)
    return handle


def _store_drop(handle):
    with _lock:
        e = _sessions.pop(handle or "", None)
        if e and e["kc_sid"] in _by_kc_sid:
            _by_kc_sid[e["kc_sid"]].discard(handle)
        return e


def _prune(now):
    for h in [h for h, e in _sessions.items()
              if now - e["last_active"] > IDLE_TIMEOUT or now - e["login_at"] > MAX_SESSION]:
        e = _sessions.pop(h)
        if e["kc_sid"] in _by_kc_sid:
            _by_kc_sid[e["kc_sid"]].discard(h)


def _keycloak_still_valid(handle, entry):
    """Present the refresh token to Keycloak. False only when Keycloak definitely
    refuses it (user disabled / deleted, SSO session ended or expired)."""
    try:
        tokens = sso.refresh(entry["refresh"])
    except OIDCError as e:
        if e.unreachable:          # Keycloak down: fail open, try again next cycle
            with _lock:
                if handle in _sessions:
                    _sessions[handle]["kc_checked"] = time.time()
            return True
        return False
    with _lock:
        if handle in _sessions:
            _sessions[handle].update(refresh=tokens.get("refresh_token", entry["refresh"]),
                                     id_token=tokens.get("id_token", entry["id_token"]), kc_checked=time.time())
    return True


def _current_employee(activity=False):
    """The signed-in employee, or (None, reason) if the session must end.
    `activity` marks a real page view (resets the idle timer); the status poll doesn't."""
    emp = session.get("employee")
    handle = session.get("handle")
    with _lock:
        entry = dict(_sessions[handle]) if emp and handle in _sessions else None
    if not entry:                  # never signed in, signed out here, or this service restarted
        return None, "expired"
    if entry.get("ended"):         # back-channel logout: the SSO session ended elsewhere
        _store_drop(handle)
        return None, "signed_out"
    now = time.time()
    if now - entry["last_active"] > IDLE_TIMEOUT or now - entry["login_at"] > MAX_SESSION:
        _store_drop(handle)
        return None, "expired"
    if now - entry["kc_checked"] > KC_CHECK_SECONDS and not _keycloak_still_valid(handle, entry):
        _store_drop(handle)
        return None, "signed_out"
    ok, reason = still_allowed(emp["id"], entry["issued_at"])
    if not ok:
        _store_drop(handle)
        return None, reason or "revoked"
    if activity:
        with _lock:
            if handle in _sessions:
                _sessions[handle]["last_active"] = now
    return emp, None


@app.get("/")
def home():
    if session.get("employee"):
        emp, reason = _current_employee(activity=True)
        if emp:
            return redirect(url_for("success"))
        session.clear()
        return redirect(url_for("home", reason=reason))
    notice = REVOKED_MESSAGES.get(request.args.get("reason"))
    return render_template("login.html", service_name=SERVICE_NAME, error=None, notice=notice)


@app.get("/login")
def login():
    """Hand the user to ServiceJourney SSO (Keycloak). If they are already signed in
    there (e.g. to the console), they come straight back without typing anything."""
    session.clear()
    try:
        return redirect(sso.begin(session))
    except OIDCError as e:
        return render_template("login.html", service_name=SERVICE_NAME, error=str(e), notice=None)


@app.get("/callback")
def callback():
    try:
        tokens, claims = sso.finish(session, request.args)
    except OIDCError as e:
        return render_template("login.html", service_name=SERVICE_NAME, error=str(e), notice=None)
    # Authorize — the one question a real service asks the platform.
    ok, reason, employee = access_check({"username": claims.get("preferred_username")}, context="login")
    if ok is None:
        error = "Service temporarily unavailable. Please try again."
    elif not ok:
        error = REVOKED_MESSAGES.get(reason, "You do not have access to this service.")
    else:
        session.clear()
        session.permanent = True                  # cookie lifetime; the limits are enforced server-side
        session["employee"] = employee
        session["handle"] = _store_put(tokens, claims)
        return redirect(url_for("success"))
    # Signed in to SSO but not allowed here: keep the SSO session, just say so.
    return render_template("login.html", service_name=SERVICE_NAME, error=error, notice=None)


@app.get("/success")
def success():
    emp, reason = _current_employee(activity=True)
    if not emp:
        session.clear()
        return redirect(url_for("home", reason=reason))
    return render_template("success.html", service_name=SERVICE_NAME, employee=emp)


@app.get("/session/status")
def session_status():
    """Polled by the success page so access changes and SSO sign-outs end live
    sessions within seconds."""
    emp, reason = _current_employee()
    if not emp:
        session.clear()
    return jsonify(authorized=bool(emp), reason=None if emp else reason)


@app.get("/logout")
def logout():
    """Single logout: ends the Keycloak session, which signs the user out of the
    console and the other service too (back-channel logout)."""
    entry = _store_drop(session.get("handle"))
    session.clear()
    return redirect(sso.logout_url(entry and entry["id_token"], "/?reason=signed_out"))


@app.post("/backchannel-logout")
def backchannel_logout():
    """Keycloak tells us (server-to-server, signed token) that an SSO session ended."""
    try:
        claims = sso.verify_logout_token(request.form.get("logout_token", ""))
    except OIDCError:
        return "", 400
    with _lock:
        for h in _by_kc_sid.pop(claims.get("sid"), set()) if claims.get("sid") else ():
            if h in _sessions:
                _sessions[h]["ended"] = True     # removed on the browser's next request (or by pruning)
    return "", 200


if __name__ == "__main__":
    app.config["SESSION_COOKIE_SECURE"] = tls_enabled()
    app.run(host="0.0.0.0", port=8080, ssl_context=ssl_context())
