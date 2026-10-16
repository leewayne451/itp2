"""Token minting and the auth decorators used by the API blueprints."""
import functools

from flask import request, jsonify, g
from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired

from . import db
from .config import SECRET_KEY, TOKEN_MAX_AGE

_serializer = URLSafeTimedSerializer(SECRET_KEY, salt="console-token")


def make_token(uid, role):
    return _serializer.dumps({"uid": uid, "role": role})


def require_auth(fn):
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        auth = request.headers.get("Authorization", "")
        if not auth.startswith("Bearer "):
            return jsonify(error="Missing bearer token"), 401
        try:
            data = _serializer.loads(auth[7:], max_age=TOKEN_MAX_AGE)
        except SignatureExpired:
            return jsonify(error="Token expired"), 401
        except BadSignature:
            return jsonify(error="Invalid token"), 401
        emp = db.query("SELECT id, username, full_name, role, status FROM employees WHERE id = %s",
                       (data.get("uid"),), fetch="one")
        if not emp or emp["status"] != "active":
            return jsonify(error="Account not active"), 401
        g.user = emp
        return fn(*args, **kwargs)

    return wrapper


def require_role(*roles):
    def deco(fn):
        @functools.wraps(fn)
        @require_auth
        def wrapper(*args, **kwargs):
            if g.user["role"] not in roles:
                return jsonify(error=f"Requires role: {', '.join(roles)}"), 403
            return fn(*args, **kwargs)
        return wrapper
    return deco
