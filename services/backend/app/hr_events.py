"""
HR-system webhook: signature check and payload validation (CHANGE 008).

Pure functions, no database or Flask imports, so they can be tested on their own.

Authentication of the HR system is an HMAC over the exact bytes it sent:

    X-HR-Timestamp: <unix seconds>
    X-HR-Signature: sha256=HMAC_SHA256(secret, "<timestamp>." + raw_body)

The timestamp is inside the MAC, so a captured request cannot be replayed later
(it is refused after MAX_SKEW_SECONDS), and the event id makes a replay *inside*
that window harmless (see api/hr.py: each event id is processed once).
"""
import hashlib
import hmac
import re
import time

EVENT_TYPES = ("hire", "transfer", "resign")
MAX_SKEW_SECONDS = 300

_EVENT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,63}$")
_USERNAME = re.compile(r"^[a-z0-9][a-z0-9._-]{1,31}$")
_EMAIL = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,190}$")


class SignatureError(Exception):
    pass


class EventError(Exception):
    pass


def sign(secret, timestamp, body):
    """The X-HR-Signature header value for `body` (bytes) sent at `timestamp`."""
    mac = hmac.new(secret.encode(), f"{timestamp}.".encode() + body, hashlib.sha256).hexdigest()
    return f"sha256={mac}"


def verify_signature(secret, timestamp, signature, body, now=None, max_skew=MAX_SKEW_SECONDS):
    """Raise SignatureError unless the signature is valid and fresh."""
    if not timestamp or not signature:
        raise SignatureError("missing signature headers")
    try:
        ts = int(timestamp)
    except (TypeError, ValueError):
        raise SignatureError("bad timestamp") from None
    if abs((now if now is not None else time.time()) - ts) > max_skew:
        raise SignatureError("timestamp outside the allowed window")
    if not hmac.compare_digest(sign(secret, timestamp, body), signature):
        raise SignatureError("signature mismatch")


def _text(value, field, *, max_len, required=True):
    value = (value or "").strip() if isinstance(value, str) or value is None else None
    if value is None:
        raise EventError(f"{field} must be text")
    if required and not value:
        raise EventError(f"{field} is required")
    if len(value) > max_len:
        raise EventError(f"{field} is too long")
    return value


def validate_event(obj):
    """Normalise a decoded webhook body, or raise EventError.

    {"event_id": "...", "type": "hire|transfer|resign", "occurred_at": "...",
     "employee": {"username": "...", "full_name": "...", "email": "..."},
     "department": "Sales", "effective_date": "YYYY-MM-DD", "reason": "..."}

    `department` is the NEW department (hire, transfer); not used for resign.
    """
    if not isinstance(obj, dict):
        raise EventError("body must be a JSON object")
    event_id = _text(obj.get("event_id"), "event_id", max_len=64)
    if not _EVENT_ID.match(event_id):
        raise EventError("event_id must be 8-64 characters: letters, digits and . _ : -")
    etype = obj.get("type")
    if etype not in EVENT_TYPES:
        raise EventError(f"type must be one of {', '.join(EVENT_TYPES)}")
    emp = obj.get("employee")
    if not isinstance(emp, dict):
        raise EventError("employee is required")
    username = _text(emp.get("username"), "employee.username", max_len=32).lower()
    if not _USERNAME.match(username):
        raise EventError("employee.username must be 2-32 characters: a-z 0-9 . _ -")
    full_name = _text(emp.get("full_name"), "employee.full_name", max_len=120, required=(etype == "hire"))
    email = _text(emp.get("email"), "employee.email", max_len=254, required=False)
    if email and not _EMAIL.match(email):
        raise EventError("employee.email is not a valid address")
    department = _text(obj.get("department"), "department", max_len=80, required=etype in ("hire", "transfer"))
    return {
        "event_id": event_id, "type": etype,
        "employee": {"username": username, "full_name": full_name, "email": email or None},
        "department": department or None,
        "occurred_at": _text(obj.get("occurred_at"), "occurred_at", max_len=40, required=False) or None,
        "effective_date": _text(obj.get("effective_date"), "effective_date", max_len=10, required=False) or None,
        "reason": _text(obj.get("reason"), "reason", max_len=500, required=False) or None,
    }
