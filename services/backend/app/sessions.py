"""
Server-side, revocable sessions.

The bearer token handed to the console is a signed wrapper around a random
256-bit session id. The database keeps only SHA-256(session id) plus metadata, so:

  * each session can be listed ("where am I signed in?") and revoked on its own;
  * "sign out everywhere" and admin "force sign-out" are one UPDATE;
  * a password change/reset, MFA reset, deactivation or role change ends the
    user's sessions immediately — not when an 8-hour token happens to expire;
  * sessions time out after 30 minutes of inactivity and 8 hours absolute;
  * a stolen database cannot be replayed as live sessions (only hashes stored),
    and a forged token is rejected by the signature before any lookup.
"""
import hashlib
import secrets

from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired

from . import db
from .clients import client_ip, client_user_agent
from .config import SECRET_KEY, SESSION_ABSOLUTE_SECONDS, SESSION_IDLE_SECONDS, SESSION_TOUCH_SECONDS

_serializer = URLSafeTimedSerializer(SECRET_KEY, salt="console-session")


def _hash(sid):
    return hashlib.sha256(sid.encode()).hexdigest()


def create(emp_id, amr, client=None):
    sid = secrets.token_urlsafe(32)
    db.query("""INSERT INTO user_sessions (sid_hash, employee_id, amr, client, ip, user_agent, expires_at)
                VALUES (%s,%s,%s,%s,%s,%s, now() + make_interval(secs => %s))""",
             (_hash(sid), emp_id, ",".join(amr), client, client_ip(), client_user_agent(),
              SESSION_ABSOLUTE_SECONDS), fetch=None)
    return _serializer.dumps({"sid": sid})


def resolve(token):
    """(session_row, None) for a live session, else (None, reason)."""
    try:
        data = _serializer.loads(token or "", max_age=SESSION_ABSOLUTE_SECONDS)
    except SignatureExpired:
        return None, "Session expired"
    except BadSignature:
        return None, "Invalid token"
    row = db.query(
        """SELECT s.*,
                  s.expires_at <= now()                                          AS abs_expired,
                  s.last_seen_at < now() - make_interval(secs => %s)             AS idle_expired,
                  s.last_seen_at < now() - make_interval(secs => %s)             AS needs_touch
           FROM user_sessions s WHERE s.sid_hash = %s""",
        (SESSION_IDLE_SECONDS, SESSION_TOUCH_SECONDS, _hash(data.get("sid") or "")), fetch="one")
    if not row or row["revoked_at"]:
        return None, "Session ended"
    if row["abs_expired"]:
        revoke(row["id"], "expired")
        return None, "Session expired"
    if row["idle_expired"]:
        revoke(row["id"], "idle timeout")
        return None, "Session timed out after inactivity"
    if row["needs_touch"]:
        db.query("UPDATE user_sessions SET last_seen_at=now() WHERE id=%s", (row["id"],), fetch=None)
    row["amr_list"] = row["amr"].split(",")
    return row, None


def add_method(session_id, method):
    """Record that this session has now also proven `method` (e.g. after opting in to TOTP)."""
    db.query("""UPDATE user_sessions SET amr = amr || ',' || %s
                WHERE id=%s AND position(%s in amr) = 0""", (method, session_id, method), fetch=None)


def revoke(session_id, reason, employee_id=None):
    """Revoke one session (optionally only if it belongs to employee_id). Returns True if it did."""
    sql = "UPDATE user_sessions SET revoked_at=now(), revoked_reason=%s WHERE id=%s AND revoked_at IS NULL"
    params = [reason, session_id]
    if employee_id is not None:
        sql += " AND employee_id=%s"
        params.append(employee_id)
    return db.query(sql + " RETURNING id", tuple(params), fetch="one") is not None


def revoke_all(employee_id, reason, except_session_id=None):
    rows = db.query("""UPDATE user_sessions SET revoked_at=now(), revoked_reason=%s
                       WHERE employee_id=%s AND revoked_at IS NULL AND id IS DISTINCT FROM %s
                       RETURNING id""", (reason, employee_id, except_session_id))
    return len(rows)


def list_active(employee_id):
    return db.query("""SELECT id, amr, client, ip, user_agent, created_at, last_seen_at, expires_at
                       FROM user_sessions
                       WHERE employee_id=%s AND revoked_at IS NULL AND expires_at > now()
                         AND last_seen_at > now() - make_interval(secs => %s)
                       ORDER BY last_seen_at DESC""", (employee_id, SESSION_IDLE_SECONDS))
