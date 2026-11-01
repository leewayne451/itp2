"""
First-party API clients (service-to-service authentication).

The console and the two protected services each hold a client secret (from .env)
and send it on every backend call as X-Client-Id / X-Client-Secret. This gives:

  * /api/service/access is no longer open to anyone — only an authenticated
    service may ask, and only about ITSELF (service1 cannot query service2);
  * the X-Forwarded-For / X-Client-User-Agent a client forwards is trusted only
    when that client authenticated, so an anonymous caller cannot spoof its IP
    to dodge rate limits or pollute the audit log.

Direct API use without client credentials still works for the user-facing
endpoints (it is just rate-limited and logged under its own address).
"""
import functools
import hmac

from flask import request, jsonify, g

from .config import API_CLIENTS


def authenticate_client():
    """before_request hook: sets g.client to the authenticated client id or None.
    Presenting WRONG credentials is an error (never silently downgraded)."""
    g.client = None
    cid = request.headers.get("X-Client-Id")
    secret = request.headers.get("X-Client-Secret")
    if cid is None and secret is None:
        return None
    expected = API_CLIENTS.get(cid or "")
    # Compare against a dummy when the id is unknown so timing does not reveal valid ids.
    ok = hmac.compare_digest((secret or "").encode(), (expected or "x" * 43).encode()) and expected is not None
    if not ok:
        return jsonify(error="Invalid client credentials"), 401
    g.client = cid
    return None


def require_client(*client_ids):
    def deco(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            if not g.get("client") or (client_ids and g.client not in client_ids):
                return jsonify(error="Client authentication required"), 401
            return fn(*args, **kwargs)
        return wrapper
    return deco


def client_ip():
    """The end user's address: forwarded by a TRUSTED client, else the socket peer."""
    if g.get("client"):
        fwd = request.headers.get("X-Forwarded-For", "").split(",")[0].strip()
        if fwd:
            return fwd[:64]
    return (request.remote_addr or "")[:64]


def client_user_agent():
    if g.get("client"):
        return (request.headers.get("X-Client-User-Agent") or "")[:300]
    return (request.headers.get("User-Agent") or "")[:300]
