"""View helpers shared across blueprints."""
import functools

from flask import session, redirect, url_for, g, abort, request

from .. import token_store


def login_required(fn):
    """Signed in = this browser holds a live server-side token entry (Keycloak SSO)."""
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        if not session.get("user") or not token_store.get(session.get("handle")):
            session.pop("user", None)
            return redirect(url_for("auth.login", next=request.full_path if request.method == "GET" else None))
        g.user = session.get("user")
        return fn(*args, **kwargs)
    return wrapper


def role_required(*roles):
    def deco(fn):
        @functools.wraps(fn)
        @login_required
        def wrapper(*args, **kwargs):
            if g.user.get("role") not in roles:
                abort(403)
            return fn(*args, **kwargs)
        return wrapper
    return deco
