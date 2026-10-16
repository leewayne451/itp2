"""Runtime configuration pulled from the environment."""
import os

EMAIL_URL = os.environ.get("EMAIL_URL", "https://email:5000")
SECRET_KEY = os.environ.get("SECRET_KEY", "poc-dev-secret-change-me")
TOKEN_MAX_AGE = 60 * 60 * 8  # signed console token lifetime: 8 hours

# --------------------------------------------------------------------------- #
# TLS. Every service serves HTTPS using the same certificate, and every
# outbound call verifies its peer against our own CA (CA_BUNDLE) — so traffic
# is encrypted AND authenticated, never `verify=False`.
# --------------------------------------------------------------------------- #
TLS_CERT = os.environ.get("TLS_CERT", "/certs/server.crt")
TLS_KEY = os.environ.get("TLS_KEY", "/certs/server.key")
CA_BUNDLE = os.environ.get("CA_BUNDLE", "/certs/ca.crt")


def ssl_context():
    """(cert, key) for Flask when the certificate is mounted, else None."""
    if os.path.exists(TLS_CERT) and os.path.exists(TLS_KEY):
        return (TLS_CERT, TLS_KEY)
    print("[tls] certificate not found — serving plain HTTP", flush=True)
    return None


def verify():
    """What `requests` should validate the peer against."""
    return CA_BUNDLE if os.path.exists(CA_BUNDLE) else True
