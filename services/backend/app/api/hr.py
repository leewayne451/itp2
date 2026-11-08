"""
HR-system webhook: joiner / mover / leaver, event-driven (CHANGE 008).

The HRMS container (services/hrms) stands in for the HR system of record (or ServiceNow
HRSD). When HR records a hire, a transfer or a resignation there, it POSTs a signed event
here and the backend raises the right request by itself:

  hire      create the employee (if new)  -> raise an ONBOARD request
  transfer                                -> raise a SHIFT request
  resign    KILL SWITCH first             -> then raise a RETURN request

The requests are raised by the "HR System (Automated)" account, so that is who the
timeline, the audit trail and the requestors' queues show. They still go through the whole
approval chain (HR -> Director -> AA -> IT -> appointment -> EUSS); only the raising is
automatic.

Kill switch (resign): before anything else the employee is set `inactive`, their access
tokens and service sessions are refused at once and Keycloak signs them out and disables the
account (retried in the background if Keycloak is down). Entitlements are deliberately left
in place so the return workflow can still see what has to be taken back; every service
denies an inactive employee regardless.

Delivery guarantees: authenticated by HMAC (hr_events.py), each event id is processed once
(replays return the stored result), and a failed event can simply be re-sent: every step
here is idempotent, so a retry after a crash resumes instead of duplicating.
"""
import json
from datetime import datetime, timezone

from flask import Blueprint, request, jsonify, g

from .. import audit, db, sync, workflows
from ..auth import require_role
from ..config import HR_WEBHOOK_SECRET, CONSOLE_URL
from ..core import add_event, has_access, service_by_type, system_actor
from ..hr_events import EventError, SignatureError, validate_event, verify_signature
from ..mailer import send_email

bp = Blueprint("hr_api", __name__)

IN_PROGRESS_SECONDS = 120   # a 'received' event older than this is assumed to have crashed


class HrReject(Exception):
    """The event can never succeed as sent (unknown employee, wrong state, ...). Final: the
    HR system must not retry it."""


@bp.post("/api/hr/events")
def receive_event():
    if len(HR_WEBHOOK_SECRET) < 32:
        return jsonify(error="HR webhook is not configured (HR_WEBHOOK_SECRET)"), 503
    raw = request.get_data(cache=True)
    try:
        verify_signature(HR_WEBHOOK_SECRET, request.headers.get("X-HR-Timestamp"),
                         request.headers.get("X-HR-Signature"), raw)
    except SignatureError as e:
        audit.record("workflow", "hr_event_signature_rejected", outcome="denied",
                     actor={"id": None, "username": "system:hrms", "role": "system"},
                     details={"reason": str(e)})
        return jsonify(error="Invalid signature"), 401
    try:
        event = validate_event(json.loads(raw))
    except (ValueError, EventError) as e:
        audit.record("workflow", "hr_event_invalid", outcome="failure",
                     actor={"id": None, "username": "system:hrms", "role": "system"},
                     details={"reason": str(e)})
        return jsonify(error=f"Invalid event: {e}"), 400

    system = system_actor()
    if not system:
        return jsonify(error="HR-system account is missing (restart the backend to seed it)"), 503
    g.user = {"id": system["id"], "username": system["username"],
              "full_name": system["full_name"], "role": system["role"]}   # audit shows the system actor

    event_row, replay = _claim(event, raw)
    if replay:
        return replay
    audit.record("workflow", "hr_event_received", target_type="employee",
                 target_label=event["employee"]["username"],
                 details={"event_id": event["event_id"], "type": event["type"]})
    try:
        result = HANDLERS[event["type"]](event, system)
    except HrReject as e:
        result = {"error": str(e)}
        _finish(event_row, "rejected", result)
        audit.record("workflow", "hr_event_rejected", outcome="failure", target_type="employee",
                     target_label=event["employee"]["username"],
                     details={"event_id": event["event_id"], "type": event["type"], "reason": str(e)})
        return jsonify(event_id=event["event_id"], status="rejected", **result), 422
    except Exception as e:  # noqa: BLE001 — leave it retryable
        result = {"error": f"{type(e).__name__}: {e}"}
        _finish(event_row, "failed", result)
        audit.record("workflow", "hr_event_failed", outcome="failure", target_type="employee",
                     target_label=event["employee"]["username"],
                     details={"event_id": event["event_id"], "type": event["type"], "error": result["error"]})
        print(f"[hr] event {event['event_id']} failed: {result['error']}", flush=True)
        return jsonify(event_id=event["event_id"], status="failed", **result), 500
    _finish(event_row, "processed", result)
    audit.record("workflow", "hr_event_processed", target_type="employee", target_label=event["employee"]["username"],
                 details={"event_id": event["event_id"], "type": event["type"], **result})
    return jsonify(event_id=event["event_id"], status="processed", **result), 200


@bp.get("/api/hr/events")
@require_role("it_admin", "admin")
def list_events():
    rows = db.query("""SELECT event_id, event_type, employee_username, status, result, received_at, processed_at
                       FROM hr_events ORDER BY id DESC LIMIT 100""")
    return jsonify([{
        "event_id": r["event_id"], "type": r["event_type"], "employee": r["employee_username"],
        "status": r["status"], "result": json.loads(r["result"]) if r["result"] else None,
        "received_at": r["received_at"].isoformat(),
        "processed_at": r["processed_at"].isoformat() if r["processed_at"] else None,
    } for r in rows])


# --------------------------------------------------------------------------- #
# Idempotency
# --------------------------------------------------------------------------- #
def _claim(event, raw):
    """(row_id, None) when this call should process the event; (None, response) when it
    is a replay (or already being processed) and the response is the answer."""
    body = raw.decode("utf-8", "replace")
    row = db.query("""INSERT INTO hr_events (event_id, event_type, employee_username, payload)
                      VALUES (%s,%s,%s,%s) ON CONFLICT (event_id) DO NOTHING RETURNING id""",
                   (event["event_id"], event["type"], event["employee"]["username"], body), fetch="one")
    if row:
        return row["id"], None
    old = db.query("""SELECT id, status, payload, result, EXTRACT(EPOCH FROM now() - received_at) AS age
                      FROM hr_events WHERE event_id=%s""", (event["event_id"],), fetch="one")
    if old["payload"] != body:
        return None, (jsonify(error="event_id was already used for a different event"), 409)
    stored = json.loads(old["result"]) if old["result"] else {}
    if old["status"] == "processed":
        return None, (jsonify(event_id=event["event_id"], status="processed", duplicate=True, **stored), 200)
    if old["status"] == "rejected":
        return None, (jsonify(event_id=event["event_id"], status="rejected", duplicate=True, **stored), 422)
    if old["status"] == "received" and old["age"] < IN_PROGRESS_SECONDS:
        return None, (jsonify(event_id=event["event_id"], status="received",
                              error="This event is being processed"), 409)
    # failed earlier (or crashed mid-way): take it over and run it again — the handlers resume.
    db.query("UPDATE hr_events SET status='received', received_at=now() WHERE id=%s", (old["id"],), fetch=None)
    return old["id"], None


def _finish(row_id, status, result):
    db.query("UPDATE hr_events SET status=%s, result=%s, processed_at=now() WHERE id=%s",
             (status, json.dumps(result, default=str), row_id), fetch=None)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _employee(username):
    return db.query("SELECT * FROM employees WHERE username=%s", (username,), fetch="one")


def _department(name):
    dept = db.query("SELECT id, name FROM departments WHERE lower(name)=lower(%s)", (name,), fetch="one")
    if not dept:
        raise HrReject(f"Unknown department '{name}'")
    return dept


def _ordinary(emp, username):
    if not emp:
        raise HrReject(f"No employee '{username}' in ServiceJourney")
    if emp["role"] != "employee":
        raise HrReject(f"'{username}' is a staff account; HR events only apply to ordinary employees")


def _prior_request(event, emp, rtype):
    """The request THIS event already raised, if an earlier attempt got that far before it
    crashed (the retry then reports it instead of failing on 'already in progress')."""
    if not emp:
        return None
    row = db.query("""SELECT id FROM requests WHERE target_employee_id=%s AND type=%s
                      AND justification LIKE %s ORDER BY id LIMIT 1""",
                   (emp["id"], rtype, f"%(event {event['event_id']})%"), fetch="one")
    return row["id"] if row else None


def _why(event):
    """Justification text carried by every request the event raises."""
    text = f"Raised automatically by the HR system (event {event['event_id']})"
    if event["reason"]:
        text += f": {event['reason']}"
    return text


# --------------------------------------------------------------------------- #
# hire -> employee + onboard
# --------------------------------------------------------------------------- #
def _hire(event, system):
    info = event["employee"]
    dept = _department(event["department"])
    emp = _employee(info["username"])
    created = False
    prior = _prior_request(event, emp, "onboard")
    if prior:   # an earlier attempt of this same event already raised it
        return {"action": "hire", "employee_id": emp["id"], "username": emp["username"], "employee_created": True,
                "request_id": prior, "request_type": "onboard", "department": dept["name"]}
    if emp:
        _ordinary(emp, info["username"])
        if emp["department_id"] is not None:
            raise HrReject(f"'{info['username']}' is already in a department; use a transfer")
        if emp["status"] != "active":
            raise HrReject(f"'{info['username']}' is {emp['status']}; re-activate the account first")
    else:
        email = info["email"] or f"{info['username']}@servicejourney.local"
        row = db.query(
            """INSERT INTO employees (username, full_name, email, role, department_id, kc_sync_pending)
               VALUES (%s,%s,%s,'employee',NULL,TRUE) RETURNING id""",
            (info["username"], info["full_name"], email), fetch="one")
        created = True
        synced = sync.push(row["id"])   # Keycloak account, NO password (see the welcome e-mail)
        audit.record("admin", "account_created", target_type="employee", target_id=row["id"],
                     target_label=info["username"],
                     details={"full_name": info["full_name"], "role": "employee", "email": email,
                              "source": f"HR event {event['event_id']}", "password": "none set (new hire sets one)",
                              **({} if synced else {"keycloak_sync": "pending (Keycloak unreachable; will retry)"})})
        send_email(email, "[ServiceJourney] Welcome — set your password",
                   f"Hello {info['full_name']},\n\nYour ServiceJourney account '{info['username']}' has been created "
                   f"following your hire in the HR system.\n\nOpen {CONSOLE_URL}, choose \"Forgot Password?\" and use "
                   f"the link we e-mail you to set your own password.")
        emp = _employee(info["username"])
    rid, err = workflows.raise_onboard(system["id"], emp, dept["id"], _why(event) + f" — onboarding into {dept['name']}")
    if err:
        raise HrReject(err)
    return {"action": "hire", "employee_id": emp["id"], "username": emp["username"], "employee_created": created,
            "request_id": rid, "request_type": "onboard", "department": dept["name"]}


# --------------------------------------------------------------------------- #
# transfer -> shift
# --------------------------------------------------------------------------- #
def _transfer(event, system):
    username = event["employee"]["username"]
    emp = _employee(username)
    _ordinary(emp, username)
    dept = _department(event["department"])
    prior = _prior_request(event, emp, "shift")
    if prior:   # an earlier attempt of this same event already raised it
        return {"action": "transfer", "employee_id": emp["id"], "username": username,
                "request_id": prior, "request_type": "shift", "department": dept["name"]}
    if emp["status"] != "active":
        raise HrReject(f"'{username}' is {emp['status']}; a transfer needs an active employee")
    rid, err = workflows.raise_shift(system["id"], emp, dept["id"], _why(event) + f" — transfer to {dept['name']}")
    if err:
        raise HrReject(err)
    return {"action": "transfer", "employee_id": emp["id"], "username": username,
            "request_id": rid, "request_type": "shift", "department": dept["name"]}


# --------------------------------------------------------------------------- #
# resign -> kill switch, then return
# --------------------------------------------------------------------------- #
def _resign(event, system):
    username = event["employee"]["username"]
    emp = _employee(username)
    _ordinary(emp, username)
    result = {"action": "resign", "employee_id": emp["id"], "username": username}

    # 1) KILL SWITCH — before any workflow work, and never rolled back by a later failure.
    result["kill_switch"] = _kill_switch(emp, event)

    # 2) Work that can no longer make sense is closed, so nobody can process it and re-activate
    #    the leaver (a processed onboard/shift sets the employee active again).
    result["closed_requests"] = _close_open_requests(emp, system, event)

    # 3) The return workflow: one request takes back the laptop + account pair.
    result["return_request"], result["return_note"] = _raise_return(emp, system, event)
    return result


def _kill_switch(emp, event):
    previous = emp["status"]
    db.query("UPDATE employees SET status='inactive' WHERE id=%s", (emp["id"],), fetch=None)
    # push() also refuses every access token / service session issued before now (immediate, even
    # with Keycloak down) and queues the Keycloak disable + sign-out for retry if it cannot be done.
    keycloak_synced = sync.push(emp["id"], end_sessions=True)
    audit.record("access", "hr_kill_switch", target_type="employee", target_id=emp["id"], target_label=emp["username"],
                 details={"event_id": event["event_id"], "previous_status": previous,
                          "signed_out_everywhere": True, "keycloak_synced": keycloak_synced,
                          "reason": event["reason"], "effective_date": event["effective_date"]})
    return {"status": "inactive", "previous_status": previous, "keycloak_synced": keycloak_synced}


def _close_open_requests(emp, system, event):
    rows = db.query("""SELECT id FROM requests WHERE target_employee_id=%s AND type <> 'return'
                       AND status NOT IN ('completed','rejected') ORDER BY id""", (emp["id"],))
    note = f"Closed automatically: employee resigned (HR event {event['event_id']})"
    for r in rows:
        db.query("UPDATE requests SET status='rejected', reject_reason=%s, updated_at=now() WHERE id=%s",
                 (note, r["id"]), fetch=None)
        add_event(r["id"], system["id"], "rejected", note)
    return [r["id"] for r in rows]


def _raise_return(emp, system, event):
    # Already has one running (e.g. a manual offboard, or this event retried): do not duplicate.
    open_return = db.query("""SELECT id FROM requests WHERE target_employee_id=%s AND type='return'
                              AND status NOT IN ('completed','rejected') ORDER BY id LIMIT 1""", (emp["id"],), fetch="one")
    if open_return:
        return open_return["id"], "a return request was already open"
    held = [r["service_id"] for r in db.query(
        "SELECT service_id FROM employee_access WHERE employee_id=%s ORDER BY service_id", (emp["id"],))]
    if not held:
        return None, "no access held, so nothing to return"
    # The account is the lower privilege: returning it takes the laptop with it and brings back
    # BOTH assets, so it is the one request that covers everything the leaver holds.
    account = service_by_type("account")
    service_id = account["id"] if account and account["id"] in held else held[0]
    rid, err = workflows.raise_return(system["id"], emp, service_id,
                                      _why(event) + " — leaver: return laptop and account")
    if err:
        raise RuntimeError(f"could not raise the return request: {err}")
    return rid, None


HANDLERS = {"hire": _hire, "transfer": _transfer, "resign": _resign}
