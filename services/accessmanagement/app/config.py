"""Console configuration from the environment."""
import os

BACKEND_URL = os.environ.get("BACKEND_URL", "https://backend:5000")
SECRET_KEY = os.environ.get("SECRET_KEY", "poc-accessmgmt-secret")
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
