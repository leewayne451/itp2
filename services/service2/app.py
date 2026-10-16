"""
Service2 — a second demo protected service.

Identical in shape to Service1: it authenticates its OWN users and then asks the
platform a single question — via /api/service/access — whether that already
identified user currently has access to THIS service. The service it represents
is selected by the SERVICE_KEY env var.

This demo has no user store of its own, so it stands in for the service's own
authentication by validating the username + password against the platform login
(/api/login), then calls /api/service/access for the access decision.
"""
import os

import requests
from flask import Flask, render_template, request, redirect, url_for, session, jsonify

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "poc-service-secret")
# Unique cookie name so this service's session does not collide with the other
# apps on localhost (cookies are scoped by host only, not by port).
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_NAME"] = os.environ.get("SESSION_COOKIE_NAME", "svc2_session")

BACKEND_URL = os.environ.get("BACKEND_URL", "https://backend:5000")
SERVICE_KEY = os.environ.get("SERVICE_KEY", "service2")
SERVICE_NAME = os.environ.get("SERVICE_NAME", "Service Two")
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
}


def authenticate(username, password):
    """Stand-in for the service's OWN user authentication. This demo has no user
    store, so it validates the credentials against the platform login endpoint.
    Returns (employee | None, reachable)."""
    try:
        resp = requests.post(
            f"{BACKEND_URL}/api/login",
            json={"username": username, "password": password},
            timeout=TIMEOUT, verify=verify(),
        )
    except requests.RequestException:
        return None, False
    if resp.status_code == 200:
        try:
            return resp.json().get("user"), True
        except ValueError:
            return None, True
    return None, True


def access_check(identity):
    """Ask the backend (source of truth) whether this identity may use this service.

    `identity` is {"username": ...} or {"employee_id": ...}. Returns
    (authorized, reason, employee): authorized is True/False when the backend
    answered, or None when it could not be reached — letting each caller choose
    to fail open or closed.
    """
    try:
        resp = requests.post(
            f"{BACKEND_URL}/api/service/access",
            json={"service": SERVICE_KEY, **identity},
            timeout=TIMEOUT, verify=verify(),
        )
        data = resp.json()
        return bool(data.get("authorized")), data.get("reason"), data.get("employee")
    except (requests.RequestException, ValueError):
        return None, "unreachable", None


def still_allowed(emp_id):
    """Live-session re-check. Fails OPEN on a backend blip so a transient error
    doesn't sign everyone out."""
    ok, reason, _ = access_check({"employee_id": emp_id})
    if ok is None:
        return True, None
    return ok, reason


@app.route("/", methods=["GET", "POST"])
def login():
    if request.method == "GET":
        if session.get("employee"):
            ok, _ = still_allowed(session["employee"]["id"])
            if ok:
                return redirect(url_for("success"))
            session.clear()
        notice = REVOKED_MESSAGES.get(request.args.get("reason"))
        return render_template("login.html", service_name=SERVICE_NAME, error=None, notice=notice)

    username = request.form.get("username", "").strip()
    password = request.form.get("password", "")
    if not username or not password:
        return render_template("login.html", service_name=SERVICE_NAME,
                               error="Enter your username and password.")

    # 1) Authenticate — the service's OWN concern (simulated via the platform login).
    employee, reachable = authenticate(username, password)
    if not reachable:
        return render_template("login.html", service_name=SERVICE_NAME,
                               error="Service temporarily unavailable. Please try again.")
    if not employee:
        return render_template("login.html", service_name=SERVICE_NAME, error="Invalid credentials.")

    # 2) Authorize — the one question a real service asks the platform.
    ok, reason, _ = access_check({"employee_id": employee["id"]})
    if ok is None:
        return render_template("login.html", service_name=SERVICE_NAME,
                               error="Service temporarily unavailable. Please try again.")
    if not ok:
        return render_template("login.html", service_name=SERVICE_NAME,
                               error=REVOKED_MESSAGES.get(reason, "You do not have access to this service."))

    session["employee"] = employee
    return redirect(url_for("success"))


@app.get("/success")
def success():
    emp = session.get("employee")
    if not emp:
        return redirect(url_for("login"))
    ok, reason = still_allowed(emp["id"])
    if not ok:
        session.clear()
        return redirect(url_for("login", reason=reason or "revoked"))
    return render_template("success.html", service_name=SERVICE_NAME, employee=emp)


@app.get("/session/status")
def session_status():
    """Polled by the success page so access changes end live sessions quickly."""
    emp = session.get("employee")
    if not emp:
        return jsonify(authorized=False, reason="expired")
    ok, reason = still_allowed(emp["id"])
    if not ok:
        session.clear()
    return jsonify(authorized=ok, reason=None if ok else (reason or "revoked"))


@app.get("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


if __name__ == "__main__":
    app.config["SESSION_COOKIE_SECURE"] = tls_enabled()
    app.run(host="0.0.0.0", port=8080, ssl_context=ssl_context())
