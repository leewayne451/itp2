"""Mandatory Job Rotation (MJR) tracker  (CHANGE 006).

MND policy: an officer is considered for rotation after ROTATION_YEARS (4) in the
same department. Before this change HR tracked that by hand and e-mailed each
Director. Here the backend:

  * keeps each officer's department start date (employees.department_since, set by
    every processed onboard / shift);
  * works out who is due (and who will be due within DUE_SOON_DAYS);
  * e-mails the Directors once per officer when they become due (background check);
  * records the Director's decision:
        keep   — with a reason; the officer comes back for review after KEEP_REVIEW_MONTHS
        rotate — with a destination department; the Requestors are told to raise the
                 shift request, which is pre-filled in their console and then follows
                 the normal approval workflow (HR -> Director -> AA -> IT -> EUSS);
  * restarts the clock when the shift is processed (new department_since).

Every decision, alert and link to a shift request is written to the audit trail.
"""
import os
import time
from datetime import date

from . import audit, db
from .mailer import send_email


def _int_env(name, default):
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


ROTATION_YEARS = _int_env("MJR_ROTATION_YEARS", 4)
DUE_SOON_DAYS = _int_env("MJR_DUE_SOON_DAYS", 90)
KEEP_REVIEW_MONTHS = _int_env("MJR_KEEP_REVIEW_MONTHS", 12)
CHECK_INTERVAL_SECONDS = _int_env("MJR_CHECK_INTERVAL_SECONDS", 3600)

SYSTEM = {"id": None, "username": "system:mjr-tracker", "role": "system"}

# Officers on the rotation clock: ordinary employees who are in a department.
# `latest` = the newest decision about the CURRENT tenure (same department_since).
_BASE = f"""
    SELECT e.id, e.username, e.full_name, e.email, e.status, e.department_id, d.name AS department_name,
           e.department_since, e.rotation_alerted_at,
           (e.department_since + make_interval(years => {ROTATION_YEARS}))::date AS due_on,
           ld.id AS decision_id, ld.decision, ld.reason AS decision_reason, ld.review_after,
           ld.decided_at, ld.target_department_id, td.name AS target_department_name,
           ld.request_id, rq.status AS request_status, dm.full_name AS decided_by_name
    FROM employees e
    JOIN departments d ON d.id = e.department_id
    LEFT JOIN LATERAL (
        SELECT * FROM rotation_decisions rd
        WHERE rd.employee_id = e.id AND rd.department_since IS NOT DISTINCT FROM e.department_since
        ORDER BY rd.decided_at DESC, rd.id DESC LIMIT 1
    ) ld ON TRUE
    LEFT JOIN departments td ON td.id = ld.target_department_id
    LEFT JOIN requests rq ON rq.id = ld.request_id
    LEFT JOIN employees dm ON dm.id = ld.decided_by
    WHERE e.role = 'employee' AND e.department_since IS NOT NULL AND e.status <> 'inactive'
"""


def _state(r, today):
    """Where the officer stands in the rotation cycle.

    due           — at/over the mark and needs a Director decision
    due_soon      — reaches the mark within DUE_SOON_DAYS
    kept          — Director decided to keep them; back for review on review_after
    rotation_planned  — Director decided to rotate; waiting for the shift request
    shift_in_progress — the shift request is going through the workflow
    ok            — nothing to do
    """
    due_on = r["due_on"]
    if r["decision"] == "rotate":
        if r["request_id"] is None:
            return "rotation_planned"
        if r["request_status"] not in ("completed", "rejected"):
            return "shift_in_progress"
        if r["request_status"] == "rejected":
            return "due"          # the move was turned down: the Director must decide again
        return "shift_in_progress"   # completed but not processed into a new tenure (should not happen)
    if r["decision"] == "keep":
        if r["review_after"] and r["review_after"] <= today:
            return "due"
        return "kept"
    if due_on <= today:
        return "due"
    if (due_on - today).days <= DUE_SOON_DAYS:
        return "due_soon"
    return "ok"


def _years_in_dept(since, today):
    years = today.year - since.year - ((today.month, today.day) < (since.month, since.day))
    months = (today.year - since.year) * 12 + today.month - since.month - (today.day < since.day)
    return years, months - years * 12


def _row_dict(r, today):
    yrs, mths = _years_in_dept(r["department_since"], today)
    return {
        "employee_id": r["id"], "username": r["username"], "full_name": r["full_name"],
        "status": r["status"], "department_id": r["department_id"], "department_name": r["department_name"],
        "department_since": r["department_since"].isoformat(),
        "due_on": r["due_on"].isoformat(), "days_to_due": (r["due_on"] - today).days,
        "tenure_years": yrs, "tenure_months": mths,
        "tenure_text": f"{yrs} yr {mths} mo",
        "state": _state(r, today),
        "alerted_at": r["rotation_alerted_at"].isoformat() if r["rotation_alerted_at"] else None,
        "decision": None if not r["decision_id"] else {
            "id": r["decision_id"], "decision": r["decision"], "reason": r["decision_reason"],
            "review_after": r["review_after"].isoformat() if r["review_after"] else None,
            "decided_at": r["decided_at"].isoformat(), "decided_by_name": r["decided_by_name"],
            "target_department_id": r["target_department_id"],
            "target_department_name": r["target_department_name"],
            "request_id": r["request_id"], "request_status": r["request_status"],
        },
    }


def officers(today=None):
    """Everyone on the rotation clock, longest-serving first."""
    today = today or date.today()
    rows = db.query(_BASE + " ORDER BY e.department_since, e.full_name")
    return [_row_dict(r, today) for r in rows]


def officer(emp_id, today=None):
    today = today or date.today()
    r = db.query(_BASE + " AND e.id = %s", (emp_id,), fetch="one")
    return _row_dict(r, today) if r else None


def settings():
    return {"rotation_years": ROTATION_YEARS, "due_soon_days": DUE_SOON_DAYS,
            "keep_review_months": KEEP_REVIEW_MONTHS}


# --------------------------------------------------------------------------- #
# E-mails
# --------------------------------------------------------------------------- #
def _people(role):
    return db.query("SELECT id, full_name, email FROM employees WHERE role=%s AND status='active' "
                    "AND email IS NOT NULL ORDER BY id", (role,))


def _officer_line(o):
    return (f"  - {o['full_name']} ({o['username']}), {o['department_name']}: in the department since "
            f"{o['department_since']} ({o['tenure_text']}); rotation due {o['due_on']}")


def check_due(force=False):
    """Find officers who are due and have not been alerted for this cycle; e-mail the
    Directors (copy to HR) one summary and mark them alerted. Returns how many."""
    today = date.today()
    fresh = []
    for o in officers(today):
        if o["state"] != "due":
            continue
        # One alert per cycle: a cycle starts at the due date, or at the end of a
        # 'keep' review period, or when a planned shift was rejected.
        cycle_start = o["due_on"]
        d = o["decision"]
        if d and d["decision"] == "keep" and d["review_after"]:
            cycle_start = d["review_after"]
        elif d and d["decision"] == "rotate":
            cycle_start = d["decided_at"][:10]
        alerted = o["alerted_at"][:10] if o["alerted_at"] else None
        if force or not alerted or alerted < str(cycle_start):
            fresh.append(o)
    if not fresh:
        return 0
    lines = "\n".join(_officer_line(o) for o in fresh)
    body = (f"Mandatory Job Rotation: {len(fresh)} officer(s) have reached the {ROTATION_YEARS}-year mark "
            f"in their department and need your decision.\n\n{lines}\n\n"
            "Open the ServiceJourney console > Due for rotation, and for each officer choose:\n"
            "  - Rotate: pick the new department. A shift request is then prepared for the Requestor, and\n"
            "    the move (with the new department's access) goes through the usual approvals.\n"
            "  - Keep: give the reason. The officer comes back for review in "
            f"{KEEP_REVIEW_MONTHS} months.\n\nThis message was generated automatically by the MJR tracker.")
    subject = f"[ServiceJourney] MJR — {len(fresh)} officer(s) due for rotation"
    sent = [send_email(p["email"], subject, body) for p in _people("director")]
    for p in _people("hr"):
        send_email(p["email"], subject + " (copy for HR)", body)
    if not any(sent):
        # No Director got it (email service down): leave the officers un-alerted so
        # the next check tries again, instead of silently losing the alert.
        print("[mjr] alert e-mail not delivered to any Director; will retry", flush=True)
        return 0
    ids = [o["employee_id"] for o in fresh]
    db.query("UPDATE employees SET rotation_alerted_at = now() WHERE id = ANY(%s)", (ids,), fetch=None)
    for o in fresh:
        audit.record("workflow", "rotation_due_notified", actor=SYSTEM, target_type="employee",
                     target_id=o["employee_id"], target_label=o["username"],
                     details={"department": o["department_name"], "department_since": o["department_since"],
                              "due_on": o["due_on"], "notified_roles": ["director", "hr"]})
    return len(fresh)


_last_check = {"at": 0.0}


def maybe_check():
    """Called from the backend's background loop; runs check_due() at most once per
    CHECK_INTERVAL_SECONDS (and once right after start-up)."""
    now = time.time()
    if now - _last_check["at"] < CHECK_INTERVAL_SECONDS:
        return 0
    _last_check["at"] = now
    n = check_due()
    if n:
        print(f"[mjr] {n} officer(s) newly due for rotation; Directors notified", flush=True)
    return n


# --------------------------------------------------------------------------- #
# Decisions
# --------------------------------------------------------------------------- #
class RotationError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.message, self.status = message, status


def decide(emp_id, decision, reason, target_department_id, actor):
    """Record a Director's keep / rotate decision for an officer who is due."""
    o = officer(emp_id)
    if not o:
        raise RotationError("Officer not found on the rotation list", 404)
    if o["status"] != "active":
        raise RotationError("This officer is not active (a transfer may already be in progress)", 409)
    if o["state"] not in ("due", "due_soon"):
        raise RotationError(f"No rotation decision is needed for this officer (status: {o['state']})", 409)
    reason = (reason or "").strip()
    review_after, target = None, None
    if decision == "keep":
        if not reason:
            raise RotationError("A reason is required to keep an officer past the rotation mark")
        review_after = db.query(
            "SELECT (CURRENT_DATE + make_interval(months => %s))::date AS d", (KEEP_REVIEW_MONTHS,), fetch="one")["d"]
    elif decision == "rotate":
        if not target_department_id:
            raise RotationError("Choose the department the officer will rotate to")
        target = db.query("SELECT id, name FROM departments WHERE id=%s", (target_department_id,), fetch="one")
        if not target:
            raise RotationError("Department not found", 404)
        if target["id"] == o["department_id"]:
            raise RotationError("The officer is already in that department")
    else:
        raise RotationError("decision must be 'keep' or 'rotate'")

    row = db.query(
        """INSERT INTO rotation_decisions (employee_id, department_id, department_since, decision, reason,
                                           target_department_id, review_after, decided_by)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
        (emp_id, o["department_id"], o["department_since"], decision, reason or None,
         target["id"] if target else None, review_after, actor["id"]), fetch="one")
    audit.record("workflow", f"rotation_{decision}", target_type="employee", target_id=emp_id,
                 target_label=o["username"],
                 details={"department": o["department_name"], "department_since": o["department_since"],
                          "tenure": o["tenure_text"], "reason": reason or None,
                          "to_department": target["name"] if target else None,
                          "review_after": review_after.isoformat() if review_after else None,
                          "decision_id": row["id"]})
    _email_decision(o, decision, reason, target, review_after, actor)
    return officer(emp_id)


def _email_decision(o, decision, reason, target, review_after, actor):
    head = (f"Officer: {o['full_name']} ({o['username']}), {o['department_name']}, "
            f"in the department since {o['department_since']} ({o['tenure_text']}).\n"
            f"Decided by: {actor.get('full_name') or actor.get('username')}\n")
    if decision == "rotate":
        subject = f"[ServiceJourney] MJR — rotate {o['full_name']} to {target['name']}: shift request to raise"
        body = (head + f"Decision: ROTATE to {target['name']}" + (f"\nNote: {reason}" if reason else "")
                + "\n\nRequestors: open the console > Rotations to raise, and submit the pre-filled department "
                  "shift request. It then follows the normal approval workflow.")
        recipients = _people("requestor") + _people("hr")
    else:
        subject = f"[ServiceJourney] MJR — {o['full_name']} kept in {o['department_name']}"
        body = (head + f"Decision: KEEP\nReason: {reason}\nNext review: {review_after}")
        recipients = _people("hr")
    for p in recipients:
        send_email(p["email"], subject, body)


def planned_rotations():
    """Rotate decisions still waiting for a Requestor to raise the shift request."""
    today = date.today()
    return [o for o in officers(today) if o["state"] == "rotation_planned"]


def link_shift(decision_id, request_id):
    """Attach a newly raised shift request to the Director's rotate decision
    (call validate_link() first)."""
    db.query("UPDATE rotation_decisions SET request_id=%s WHERE id=%s", (request_id, decision_id), fetch=None)


def validate_link(decision_id, emp_id):
    """Pre-check (before the shift request is created) — same rules as link_shift."""
    d = db.query("""SELECT rd.*, e.department_since AS current_since FROM rotation_decisions rd
                    JOIN employees e ON e.id = rd.employee_id WHERE rd.id=%s""", (decision_id,), fetch="one")
    if not d or d["employee_id"] != emp_id:
        return "That rotation decision does not belong to this employee"
    if d["decision"] != "rotate" or d["department_since"] != d["current_since"]:
        return "That rotation decision is no longer current"
    if d["request_id"] is not None:
        return "A shift request has already been raised for this rotation"
    return None
