"""View helpers shared across blueprints."""
import functools

from flask import session, redirect, url_for, g, abort


def login_required(fn):
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        if not session.get("token"):
            return redirect(url_for("auth.login"))
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
