"""
Request creation for AUTOMATED flows (the HR-system webhook, CHANGE 008).

These mirror what POST /api/requests does for a human Requestor (same checks, same
first state, same side effects) but take the requestor as a parameter instead of
reading it from the signed-in user. Each returns (request_id, None) or (None, error).
"""
from datetime import datetime

from . import db, sync
from .core import add_event, has_access
from .tracker_logic import SGT


def today_sgt():
    return datetime.now(SGT).date()


def nice_date(d):
    """2026-11-27 -> 'Fri 27 Nov 2026' (for request notes and e-mails)."""
    return f"{d:%a} {d.day} {d:%b %Y}"


def _open_request(target_id, rtype, service_id=None):
    sql = "SELECT id FROM requests WHERE target_employee_id=%s AND type=%s AND status NOT IN ('completed','rejected')"
    params = [target_id, rtype]
    if service_id is not None:
        sql += " AND service_id=%s"
        params.append(service_id)
    return db.query(sql, tuple(params), fetch="one")


def raise_onboard(requestor_id, target, dept_id, justification, on_behalf=True):
    if target["department_id"] is not None:
        return None, "Onboarding is only for employees not yet in a department"
    if not db.query("SELECT 1 FROM departments WHERE id=%s", (dept_id,), fetch="one"):
        return None, "Department not found"
    if _open_request(target["id"], "onboard"):
        return None, "An onboarding for this employee is already in progress"
    row = db.query(
        """INSERT INTO requests (type, requestor_id, target_employee_id, target_department_id,
                                 on_behalf, justification, status)
           VALUES ('onboard',%s,%s,%s,%s,%s,'pending_hr') RETURNING id""",
        (requestor_id, target["id"], dept_id, on_behalf, justification), fetch="one")
    add_event(row["id"], requestor_id, "submitted", "Onboarding requested")
    return row["id"], None


def raise_shift(requestor_id, target, dept_id, justification, on_behalf=True, effective_date=None):
    """effective_date (CHANGE 013): the day the transfer takes effect. A future date lets
    the approvals run now while the employee keeps their current access; the suspension
    happens on that date (start_due_transfers) and IT can process it from that date."""
    if target["department_id"] is None:
        return None, "Employee has no department yet; onboard them first"
    if not db.query("SELECT 1 FROM departments WHERE id=%s", (dept_id,), fetch="one"):
        return None, "Department not found"
    if target["department_id"] == dept_id:
        return None, "Employee is already in that department"
    if _open_request(target["id"], "shift"):
        return None, "A shift for this employee is already in progress"
    if effective_date and effective_date < today_sgt():
        return None, "The transfer date is in the past"
    later = bool(effective_date and effective_date > today_sgt())
    row = db.query(
        """INSERT INTO requests (type, requestor_id, target_employee_id, target_department_id,
                                 source_department_id, on_behalf, justification, status,
                                 effective_date, transfer_started_at)
           VALUES ('shift',%s,%s,%s,%s,%s,%s,'pending_hr',%s,%s) RETURNING id""",
        (requestor_id, target["id"], dept_id, target["department_id"], on_behalf, justification,
         effective_date, None if later else datetime.now(SGT)), fetch="one")
    if later:
        add_event(row["id"], requestor_id, "submitted",
                  f"Department shift requested for {nice_date(effective_date)}; current services "
                  "stay until then, and IT switches them on that date")
        return row["id"], None
    _suspend_for_transfer(target["id"])
    add_event(row["id"], requestor_id, "submitted",
              "Department shift requested; current services suspended pending transfer")
    return row["id"], None


def _suspend_for_transfer(emp_id):
    db.query("UPDATE employees SET status='suspended' WHERE id=%s AND status='active'", (emp_id,), fetch=None)
    sync.push(emp_id, end_sessions=True)   # Keycloak: account disabled, signed out everywhere


def start_due_transfers(today=None):
    """On the transfer date, suspend the employee of every dated shift that IT has not
    processed yet (CHANGE 013). Claimed atomically, so it runs once per request. Returns
    the request ids it started."""
    today = today or today_sgt()
    rows = db.query(
        """UPDATE requests r SET transfer_started_at=now(), updated_at=now()
             FROM employees e
            WHERE r.type='shift' AND r.effective_date IS NOT NULL AND r.effective_date<=%s
              AND r.transfer_started_at IS NULL
              AND r.status IN ('pending_hr','pending_director','pending_aa','pending_it')
              AND e.id=r.target_employee_id AND e.status='active'
        RETURNING r.id, r.target_employee_id""", (today,))
    for r in rows:
        _suspend_for_transfer(r["target_employee_id"])
        add_event(r["id"], None, "transfer_started", "Transfer date reached; current services suspended pending transfer",
                  background=True)
    return [r["id"] for r in rows]


def raise_return(requestor_id, target, service_id, justification, on_behalf=True, return_by=None):
    """return_by (CHANGE 013): the leaver's last physical day; the EUSS appointment is
    matched on or before it."""
    if not has_access(target["id"], service_id):
        return None, "Target does not have access to this service"
    if _open_request(target["id"], "return", service_id):
        return None, "An open request of this type already exists for that employee/service"
    row = db.query(
        """INSERT INTO requests (type, requestor_id, target_employee_id, service_id, on_behalf, justification,
                                 status, return_by)
           VALUES ('return',%s,%s,%s,%s,%s,'pending_hr',%s) RETURNING id""",
        (requestor_id, target["id"], service_id, on_behalf, justification, return_by), fetch="one")
    add_event(row["id"], requestor_id, "submitted", "Return (offboard) request submitted"
              + (f"; return by {nice_date(return_by)} (last physical day)" if return_by else ""))
    return row["id"], None
