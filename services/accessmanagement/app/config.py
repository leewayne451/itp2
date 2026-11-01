"""Console configuration from the environment."""
import os

BACKEND_URL = os.environ.get("BACKEND_URL", "https://backend:5000")


def _required_secret(name):
    """No hard-coded fallback: refuse to start without a strong secret from .env."""
    value = os.environ.get(name, "")
    if len(value) < 32 or "change-me" in value:
        raise RuntimeError(f"{name} is missing or too weak. Run `python scripts/generate_secrets.py`, "
                           f"then `docker compose up -d --force-recreate`.")
    return value


SECRET_KEY = _required_secret("SECRET_KEY")
# This app's client credential towards the backend (service-to-service auth).
CLIENT_ID = os.environ.get("CLIENT_ID", "console")
CLIENT_SECRET = _required_secret("CLIENT_SECRET")
# The console cookie expires after this much inactivity (Keycloak's SSO session has
# its own, authoritative idle timeout of the same length).
IDLE_TIMEOUT_MINUTES = int(os.environ.get("IDLE_TIMEOUT_MINUTES", 30))

# Single sign-on: this app's OpenID Connect client registration in Keycloak.
OIDC_CLIENT_ID = os.environ.get("OIDC_CLIENT_ID", "console")
OIDC_CLIENT_SECRET = _required_secret("OIDC_CLIENT_SECRET")
APP_URL = os.environ.get("APP_URL", "https://localhost:8080").rstrip("/")
# Distinct cookie name: browser cookies are scoped by host only (not port), so
# without this the console and the service dockers on localhost would clobber
# each other's session cookie.
SESSION_COOKIE_NAME = "am_session"

# --------------------------------------------------------------------------- #
# TLS — serve HTTPS, and verify the backend against our own CA.
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
