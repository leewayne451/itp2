"""Runtime configuration pulled from the environment."""
import os

EMAIL_URL = os.environ.get("EMAIL_URL", "https://email:5000")


def _required_secret(name, min_len=32):
    """Secrets come from the environment (.env, see scripts/generate_secrets.py).
    There is deliberately NO hard-coded fallback: the service refuses to start
    rather than run with a guessable key."""
    value = os.environ.get(name, "")
    if len(value) < min_len or "change-me" in value:
        raise RuntimeError(f"{name} is missing or too weak. Run `python scripts/generate_secrets.py` "
                           f"to create .env, then `docker compose up -d --force-recreate`.")
    return value


# --------------------------------------------------------------------------- #
# Single sign-on: Keycloak is the identity provider (see keycloak.py / tokens.py)
# --------------------------------------------------------------------------- #
# Two URLs for the same server: browsers use the PUBLIC one (it is the token
# issuer), containers reach it on the INTERNAL docker-network name.
KEYCLOAK_URL = os.environ.get("KEYCLOAK_URL", "https://keycloak:8443").rstrip("/")
KEYCLOAK_PUBLIC_URL = os.environ.get("KEYCLOAK_PUBLIC_URL", "https://localhost:8443").rstrip("/")
KEYCLOAK_REALM = os.environ.get("KEYCLOAK_REALM", "servicejourney")
KEYCLOAK_ISSUER = f"{KEYCLOAK_PUBLIC_URL}/realms/{KEYCLOAK_REALM}"
KEYCLOAK_JWKS_URL = f"{KEYCLOAK_URL}/realms/{KEYCLOAK_REALM}/protocol/openid-connect/certs"
# The backend's own service account (user sync, admin actions, event ingestion).
KEYCLOAK_CLIENT_ID = os.environ.get("KEYCLOAK_BACKEND_CLIENT_ID", "servicejourney-backend")
KEYCLOAK_CLIENT_SECRET = _required_secret("KEYCLOAK_BACKEND_CLIENT_SECRET")
# Access tokens accepted by this API must be issued FOR it (aud) BY one of these apps (azp).
API_AUDIENCE = "servicejourney-api"
API_AUTHORIZED_PARTIES = {"console"}
STAFF_GROUP = "staff"            # Keycloak group whose members must use TOTP
SYNC_INTERVAL_SECONDS = 10       # retry Keycloak sync / pull Keycloak events this often

# --------------------------------------------------------------------------- #
# First-party API clients (service-to-service authentication)
# --------------------------------------------------------------------------- #
# "console=<secret>,service1=<secret>,service2=<secret>". Clients send
# X-Client-Id / X-Client-Secret. Only authenticated clients may call
# /api/service/access, and only their forwarded client IP is trusted.
API_CLIENTS = {}
for _pair in os.environ.get("API_CLIENTS", "").split(","):
    if "=" in _pair:
        _cid, _sec = _pair.split("=", 1)
        if _cid.strip() and len(_sec.strip()) >= 32:
            API_CLIENTS[_cid.strip()] = _sec.strip()

# --------------------------------------------------------------------------- #
# Audit trail (see audit.py)
# --------------------------------------------------------------------------- #
# HMAC key that seals the audit hash chain: without it, nobody with database
# access can rewrite an entry AND recompute a valid chain.
AUDIT_HMAC_KEY = _required_secret("AUDIT_HMAC_KEY")
# Second, append-only copy of every entry as JSON Lines (one file per UTC day) on
# the backend's own volume — SIEM-friendly and used to detect a truncated table.
AUDIT_LOG_DIR = os.environ.get("AUDIT_LOG_DIR", "/var/log/servicejourney")

# --------------------------------------------------------------------------- #
# Multi-factor authentication (TOTP)
# --------------------------------------------------------------------------- #
# Roles that MUST use TOTP: their accounts are put in Keycloak's "staff" group, whose
# login flow requires a code. Others (employees) may opt in from their Keycloak
# account page. Comma-separated; override with the env var.
MFA_REQUIRED_ROLES = {
    r.strip() for r in os.environ.get(
        "MFA_REQUIRED_ROLES", "admin,requestor,hr,director,aa,it_admin,euss").split(",") if r.strip()
}
# Fernet key that encrypted TOTP secrets before Keycloak took over two-factor.
# Only needed to MIGRATE existing enrolments into Keycloak; optional otherwise.
MFA_ENC_KEY = os.environ.get("MFA_ENC_KEY", "")

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
