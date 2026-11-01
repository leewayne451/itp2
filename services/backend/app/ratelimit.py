"""
Per-client-IP rate limiting for the credential endpoints (sliding window).

Complements the per-ACCOUNT lockout in passwords.py: the lockout stops guessing
one user's password; this stops one source spraying many accounts, hammering
MFA codes, or flooding the forgot-password mailer.

In-memory and per-process — right for this single-process POC. Behind several
workers you would keep the counters in Redis (e.g. Flask-Limiter + Redis).
"""
import functools
import threading
import time
from collections import defaultdict, deque

from flask import jsonify

from .clients import client_ip

_lock = threading.Lock()
_hits = defaultdict(deque)


def _allow(key, limit, window):
    now = time.monotonic()
    with _lock:
        q = _hits[key]
        while q and q[0] <= now - window:
            q.popleft()
        if len(q) >= limit:
            return False, int(window - (now - q[0])) + 1
        q.append(now)
        return True, 0


def rate_limited(bucket, limit, window_seconds):
    """Allow at most `limit` calls per `window_seconds` per client IP."""
    def deco(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            ok, retry = _allow((bucket, client_ip()), limit, window_seconds)
            if not ok:
                resp = jsonify(error=f"Too many attempts. Please wait {retry} seconds and try again.",
                               retry_after=retry)
                resp.status_code = 429
                resp.headers["Retry-After"] = str(retry)
                return resp
            return fn(*args, **kwargs)
        return wrapper
    return deco

