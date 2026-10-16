"""Thin client the views use to talk to the backend API with the session token."""
import requests
from flask import session, current_app

from .config import verify


class AuthExpired(Exception):
    """Raised when the backend rejects our token (401); handled by an errorhandler."""


def _url(path):
    return current_app.config["BACKEND_URL"].rstrip("/") + path


def login(username, password):
    return requests.post(_url("/api/login"), json={"username": username, "password": password},
                         timeout=10, verify=verify())


def call(method, path, json=None, params=None):
    headers = {}
    token = session.get("token")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    resp = requests.request(method, _url(path), headers=headers, json=json, params=params,
                            timeout=15, verify=verify())
    if resp.status_code == 401:
        raise AuthExpired()
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
