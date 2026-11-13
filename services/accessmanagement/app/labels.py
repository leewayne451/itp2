"""Presentation helpers exposed to Jinja templates."""
from datetime import datetime, timedelta, timezone

# Every time shown to users is Singapore time (UTC+8, no daylight saving). The database
# and the audit trail keep UTC; only the display is converted.
SGT = timezone(timedelta(hours=8), "SGT")

STATUS_LABEL = {
    "pending_hr": "Pending HR approval",
    "pending_director": "Pending Director approval",
    "pending_aa": "Pending Approving Authority",
    "pending_it": "Pending IT processing",
    "pending_appointment": "Awaiting appointment booking",
    "pending_euss": "Awaiting EUSS",
    "completed": "Completed",
    "rejected": "Rejected",
}
ROLE_LABEL = {
    "requestor": "Requestor", "hr": "HR", "director": "Director", "aa": "Approving Authority",
    "it_admin": "IT Admin", "euss": "EUSS", "admin": "Administrator", "employee": "Employee",
}
TYPE_LABEL = {"grant": "Grant", "return": "Return", "shift": "Department shift", "onboard": "Onboarding"}
ACTION_LABEL = {
    "approved": "Approved", "rejected": "Rejected", "processed": "Processed",
    "provisioned": "Provisioned", "retrieved": "Retrieved",
    "appointment_booked": "Booked appt", "submitted": "Submitted",
    "appointment_rescheduled": "Moved appt", "appointment_cancelled": "Appt cancelled",
}


# Mandatory Job Rotation states (CHANGE 006): label + pill colour.
ROTATION_STATE = {
    "due": ("Due — decision needed", "bad"),
    "due_soon": ("Due soon", "wait"),
    "rotation_planned": ("Rotate — shift to be raised", "wait"),
    "shift_in_progress": ("Shift in progress", "wait"),
    "kept": ("Kept", "ok"),
    "ok": ("Within term", "ok"),
}


def rotation_state(s):
    return ROTATION_STATE.get(s, (s, "wait"))


def status_label(s):
    return STATUS_LABEL.get(s, s)


def status_class(s):
    return "ok" if s == "completed" else "bad" if s == "rejected" else "wait"


def to_sgt(iso):
    """ISO timestamp (any offset; naive = UTC) -> aware datetime in Singapore time."""
    d = datetime.fromisoformat(iso)
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return d.astimezone(SGT)


def fmt_dt(iso, seconds=False):
    if not iso:
        return "—"
    try:
        return to_sgt(iso).strftime("%Y-%m-%d %H:%M:%S" if seconds else "%Y-%m-%d %H:%M")
    except (ValueError, TypeError):
        return iso


def fmt_day(iso):
    """'Tue 17 Nov, 10:00' — the tracker's friendlier date (CHANGE 011)."""
    if not iso:
        return "—"
    try:
        return to_sgt(iso).strftime("%a %d %b, %H:%M")
    except (ValueError, TypeError):
        return iso


def fmt_hm(iso):
    try:
        return to_sgt(iso).strftime("%H:%M") if iso else ""
    except (ValueError, TypeError):
        return iso


def req_resource(r):
    if r["type"] == "onboard":
        return f"onboard → {r.get('new_department_name') or '?'}"
    if r["type"] == "shift":
        return f"{r.get('source_department_name') or '?'} → {r.get('new_department_name') or '?'}"
    return r.get("service_name") or "—"


def behalf_label(r):
    return "on behalf" if r.get("on_behalf") else "self"


def register(app):
    app.jinja_env.filters["dt"] = fmt_dt
    app.jinja_env.filters["dts"] = lambda iso: fmt_dt(iso, seconds=True)
    app.jinja_env.filters["dday"] = fmt_day
    app.jinja_env.filters["hm"] = fmt_hm
    app.jinja_env.globals.update(
        status_label=status_label, status_class=status_class, req_resource=req_resource,
        behalf_label=behalf_label, rotation_state=rotation_state, ROLE_LABEL=ROLE_LABEL, TYPE_LABEL=TYPE_LABEL, ACTION_LABEL=ACTION_LABEL,
    )
