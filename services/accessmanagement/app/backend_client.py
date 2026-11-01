"""Thin client the views use to talk to the backend API with the user's Keycloak
access token (kept server-side in token_store)."""
import time

import requests
from flask import session, current_app, request

from .config import verify, CLIENT_ID, CLIENT_SECRET


class AuthExpired(Exception):
    """Raised when the backend rejects our token (401); handled by an errorhandler."""

    def __init__(self, reason=None):
        super().__init__(reason)
        self.reason = reason


def _url(path):
    return current_app.config["BACKEND_URL"].rstrip("/") + path


def _forwarded():
    """Our client credential (service-to-service auth) plus the end user's address
    and browser, which the backend trusts ONLY because we authenticated."""
    return {"X-Client-Id": CLIENT_ID, "X-Client-Secret": CLIENT_SECRET,
            "X-Forwarded-For": request.remote_addr or "",
            "X-Client-User-Agent": (request.headers.get("User-Agent") or "")[:300]}


def _access_token():
    """The signed-in user's access token, refreshed via Keycloak when about to expire."""
    from . import token_store
    from .oidc_client import client
    handle = session.get("handle")
    entry = token_store.get(handle)
    if not entry:
        raise AuthExpired("Your session has ended")
    if entry["access_exp"] - time.time() < 30:
        try:
            tokens = client().refresh(entry["refresh"])
        except Exception as e:  # noqa: BLE001 — refresh token expired / SSO session ended
            token_store.drop(handle)
            raise AuthExpired("Your session has ended") from e
        exp = client().access_claims(tokens["access_token"]).get("exp", 0)
        token_store.update(handle, tokens, exp)
        return tokens["access_token"]
    return entry["access"]


def call(method, path, json=None, params=None, token=None):
    headers = _forwarded()
    headers["Authorization"] = f"Bearer {token or _access_token()}"
    resp = requests.request(method, _url(path), headers=headers, json=json, params=params,
                            timeout=15, verify=verify())
    if resp.status_code == 401:
        try:
            reason = resp.json().get("error")
        except ValueError:
            reason = None
        raise AuthExpired(reason)
    return resp


def get_json(path, params=None, default=None):
    try:
        resp = call("GET", path, params=params)
    except requests.RequestException:
        return default
    if not resp.ok:
        return default
    try:
        return resp.json()
    except ValueError:
        return default


def error_of(resp):
    """Pull a human error message out of a backend response."""
    try:
        return resp.json().get("error") or f"Request failed ({resp.status_code})"
    except ValueError:
        return f"Request failed ({resp.status_code})"
