"""Immediate and scheduled leaver cut-off.

The backend is authoritative, so setting the employee inactive and advancing the
token cut-off blocks access even when Keycloak is unavailable. Entitlements remain
as recovery evidence until the automated Return request reaches EUSS, matching the
existing HRMS resignation lifecycle.

A resignation from the HRMS (CHANGE 013) carries a last working day and a last physical
day. Access is cut at the end of the last working day (00:00 Singapore time the next
day); the Return request is raised straight away instead, so it can be approved and its
EUSS appointment booked on or before the last physical day.
"""
import threading
import time

from . import audit, db, scheduling, sync, workflows
from .config import OFFBOARDING_INTERVAL_SECONDS
from .core import add_event, service_by_type, system_actor

SYSTEM = {"id": None, "username": "scheduled-offboarding-worker", "role": "system"}


class OffboardingError(ValueError):
    pass


def _employee(emp_id):
    return db.query("SELECT * FROM employees WHERE id=%s", (emp_id,), fetch="one")


def schedule(emp_id, effective_at, reason, actor):
    emp = _employee(emp_id)
    if not emp:
        raise OffboardingError("Employee not found")
    if emp["role"] != "employee":
        raise OffboardingError("Only ordinary employees can be offboarded")
    if emp["status"] == "inactive" or emp.get("offboarded_at"):
        raise OffboardingError("This employee is already inactive")
    db.query(
        """UPDATE employees SET scheduled_offboard_at=%s,scheduled_offboard_reason=%s,
                   scheduled_offboard_by_id=%s WHERE id=%s""",
        (effective_at, reason, actor.get("id"), emp_id), fetch=None)
    audit.record("access", "offboarding_scheduled", actor=actor, target_type="employee",
                 target_id=emp_id, target_label=emp["username"],
                 details={"effective_at": effective_at.isoformat(), "reason": reason})
    return _employee(emp_id)


def cancel_schedule(emp_id, reason, actor):
    emp = _employee(emp_id)
    if not emp:
        raise OffboardingError("Employee not found")
    if not emp.get("scheduled_offboard_at") or emp.get("offboarded_at"):
        raise OffboardingError("This employee has no pending scheduled offboarding")
    previous = emp["scheduled_offboard_at"]
    db.query("""UPDATE employees SET scheduled_offboard_at=NULL,scheduled_offboard_reason=NULL,
                     scheduled_offboard_by_id=NULL WHERE id=%s""", (emp_id,), fetch=None)
    withdrawn = _withdraw_early_return(emp_id, actor, reason)
    audit.record("access", "offboarding_schedule_cancelled", actor=actor,
                 target_type="employee", target_id=emp_id, target_label=emp["username"],
                 details={"was_effective_at": previous.isoformat(), "reason": reason,
                          "withdrawn_return_request_ids": withdrawn})
    return withdrawn


def _withdraw_early_return(emp_id, actor, reason):
    """The resignation is off: close the Return request it raised ahead of the cut-off
    (the one carrying a return-by date) and free its EUSS place."""
    rows = db.query("""SELECT id FROM requests WHERE target_employee_id=%s AND type='return'
                        AND return_by IS NOT NULL AND justification LIKE 'Raised automatically by HRMS offboarding%%'
                        AND status NOT IN ('completed','rejected')""", (emp_id,))
    note = f"Resignation cancelled: {reason}"
    for row in rows:
        db.query("""UPDATE requests SET status='rejected',reject_reason=%s,updated_at=now(),
                         closed_at=COALESCE(closed_at,now()) WHERE id=%s""", (note, row["id"]), fetch=None)
        scheduling.cancel_booking(row["id"])
        add_event(row["id"], actor.get("id"), "rejected", note, background=True)
    return [row["id"] for row in rows]


def raise_early_return(emp_id, actor, reason, return_by):
    """Raise the leaver's Return request now, due by their last physical day."""
    emp = _employee(emp_id)
    if not emp:
        raise OffboardingError("Employee not found")
    return _raise_return(emp, actor, reason, "HRMS", return_by=return_by)


def _close_open_requests(emp_id, actor, reason):
    rows = db.query("""SELECT id FROM requests WHERE target_employee_id=%s
                        AND type <> 'return'
                        AND status NOT IN ('completed','rejected') ORDER BY id""", (emp_id,))
    note = f"Closed by offboarding: {reason}"
    for row in rows:
        db.query("""UPDATE requests SET status='rejected',reject_reason=%s,updated_at=now(),
                         closed_at=COALESCE(closed_at,now()) WHERE id=%s""",
                 (note, row["id"]), fetch=None)
        scheduling.cancel_booking(row["id"])
        add_event(row["id"], actor.get("id"), "rejected", note, background=True)
    return [row["id"] for row in rows]


def _raise_return(emp, actor, reason, trigger, return_by=None):
    """Raise one automated Return request covering the leaver's held resources."""
    existing = db.query(
        """SELECT id, return_by FROM requests WHERE target_employee_id=%s AND type='return'
             AND status NOT IN ('completed','rejected') ORDER BY id LIMIT 1""",
        (emp["id"],), fetch="one")
    if existing:
        if return_by and existing["return_by"] is None:
            db.query("UPDATE requests SET return_by=%s WHERE id=%s", (return_by, existing["id"]), fetch=None)
        return existing["id"], "a return request was already open"

    held = [row["service_id"] for row in db.query(
        "SELECT service_id FROM employee_access WHERE employee_id=%s ORDER BY service_id",
        (emp["id"],))]
    if not held:
        return None, "no access held, so nothing to return"
    automation = system_actor()
    if not automation:
        raise RuntimeError("HR-system automation account is missing")
    account = service_by_type("account")
    service_id = account["id"] if account and account["id"] in held else held[0]
    justification = (f"Raised automatically by {trigger} offboarding, initiated by "
                     f"{actor.get('username') or 'system'}: {reason}")
    rid, error = workflows.raise_return(automation["id"], emp, service_id,
                                        justification + " — leaver: return laptop and account",
                                        return_by=return_by)
    if error:
        raise RuntimeError(f"could not raise the return request: {error}")
    return rid, None


def execute(emp_id, reason, actor, trigger="emergency"):
    """Apply the cut-off idempotently and return an execution summary."""
    emp = _employee(emp_id)
    if not emp:
        raise OffboardingError("Employee not found")
    if emp["role"] != "employee":
        raise OffboardingError("Only ordinary employees can be offboarded")
    if emp.get("offboarded_at"):
        return {"already_offboarded": True, "employee_id": emp_id,
                "offboarded_at": emp["offboarded_at"].isoformat()}

    previous_status = emp["status"]
    # This is the immediate fail-closed control. Everything after it is retryable.
    db.query("UPDATE employees SET status='inactive' WHERE id=%s", (emp_id,), fetch=None)
    keycloak_synced = sync.push(emp_id, end_sessions=True)

    services = [r["service_id"] for r in db.query(
        "SELECT service_id FROM employee_access WHERE employee_id=%s ORDER BY service_id", (emp_id,))]
    closed = _close_open_requests(emp_id, actor, reason)
    return_request, return_note = _raise_return(emp, actor, reason, trigger)
    row = db.query(
        """UPDATE employees SET offboarded_at=now(),offboarded_by_id=%s,
                   offboard_reason=%s,offboard_trigger=%s
             WHERE id=%s AND offboarded_at IS NULL RETURNING offboarded_at""",
        (actor.get("id"), reason, trigger, emp_id), fetch="one")
    finished_at = row["offboarded_at"] if row else _employee(emp_id)["offboarded_at"]
    details = {"trigger": trigger, "reason": reason, "previous_status": previous_status,
               "retained_service_ids": services, "closed_request_ids": closed,
               "return_request_id": return_request, "return_note": return_note,
               "signed_out_everywhere": True, "keycloak_synced": keycloak_synced}
    audit.record("access", "employee_offboarded", actor=actor, target_type="employee",
                 target_id=emp_id, target_label=emp["username"], details=details)
    return {"employee_id": emp_id, "offboarded_at": finished_at.isoformat(), **details}


def execute_due(limit=50):
    rows = db.query(
        """SELECT id,scheduled_offboard_reason FROM employees
            WHERE scheduled_offboard_at IS NOT NULL AND scheduled_offboard_at<=now()
              AND offboarded_at IS NULL ORDER BY scheduled_offboard_at,id LIMIT %s""", (limit,))
    completed = 0
    for row in rows:
        execute(row["id"], row["scheduled_offboard_reason"] or "Scheduled leaver cut-off",
                SYSTEM, trigger="scheduled")
        completed += 1
    return completed


def start_background():
    def loop():
        while True:
            try:
                count = execute_due()
                if count:
                    print(f"[offboarding] completed {count} scheduled cut-off(s)", flush=True)
            except Exception as exc:  # noqa: BLE001 -- retry on the next cycle
                print(f"[offboarding] background error: {type(exc).__name__}: {exc}", flush=True)
            try:   # dated transfers (CHANGE 013): suspend on the transfer date
                started = workflows.start_due_transfers()
                if started:
                    print(f"[transfers] started {len(started)} dated transfer(s)", flush=True)
            except Exception as exc:  # noqa: BLE001 -- retry on the next cycle
                print(f"[transfers] background error: {type(exc).__name__}: {exc}", flush=True)
            time.sleep(OFFBOARDING_INTERVAL_SECONDS)

    print(f"[offboarding] worker interval={OFFBOARDING_INTERVAL_SECONDS}s", flush=True)
    threading.Thread(target=loop, name="offboarding", daemon=True).start()
