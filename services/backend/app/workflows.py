"""
Request creation for AUTOMATED flows (the HR-system webhook, CHANGE 008).

These mirror what POST /api/requests does for a human Requestor (same checks, same
first state, same side effects) but take the requestor as a parameter instead of
reading it from the signed-in user. Each returns (request_id, None) or (None, error).
"""
from . import db, sync
from .core import add_event, has_access


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


def raise_shift(requestor_id, target, dept_id, justification, on_behalf=True):
    if target["department_id"] is None:
        return None, "Employee has no department yet; onboard them first"
    if not db.query("SELECT 1 FROM departments WHERE id=%s", (dept_id,), fetch="one"):
        return None, "Department not found"
    if target["department_id"] == dept_id:
        return None, "Employee is already in that department"
    if _open_request(target["id"], "shift"):
        return None, "A shift for this employee is already in progress"
    row = db.query(
        """INSERT INTO requests (type, requestor_id, target_employee_id, target_department_id,
                                 source_department_id, on_behalf, justification, status)
           VALUES ('shift',%s,%s,%s,%s,%s,%s,'pending_hr') RETURNING id""",
        (requestor_id, target["id"], dept_id, target["department_id"], on_behalf, justification), fetch="one")
    db.query("UPDATE employees SET status='suspended' WHERE id=%s", (target["id"],), fetch=None)
    sync.push(target["id"], end_sessions=True)   # Keycloak: account disabled, signed out everywhere
    add_event(row["id"], requestor_id, "submitted",
              "Department shift requested; current services suspended pending transfer")
    return row["id"], None


def raise_return(requestor_id, target, service_id, justification, on_behalf=True):
    if not has_access(target["id"], service_id):
        return None, "Target does not have access to this service"
    if _open_request(target["id"], "return", service_id):
        return None, "An open request of this type already exists for that employee/service"
    row = db.query(
        """INSERT INTO requests (type, requestor_id, target_employee_id, service_id, on_behalf, justification, status)
           VALUES ('return',%s,%s,%s,%s,%s,'pending_hr') RETURNING id""",
        (requestor_id, target["id"], service_id, on_behalf, justification), fetch="one")
    add_event(row["id"], requestor_id, "submitted", "Return (offboard) request submitted")
    return row["id"], None
