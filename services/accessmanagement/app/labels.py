"""Presentation helpers exposed to Jinja templates."""
from datetime import datetime

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
}


def status_label(s):
    return STATUS_LABEL.get(s, s)


def status_class(s):
    return "ok" if s == "completed" else "bad" if s == "rejected" else "wait"


def fmt_dt(iso):
    if not iso:
        return "—"
    try:
        return datetime.fromisoformat(iso).strftime("%Y-%m-%d %H:%M")
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
    app.jinja_env.globals.update(
        status_label=status_label, status_class=status_class, req_resource=req_resource,
        behalf_label=behalf_label, ROLE_LABEL=ROLE_LABEL, TYPE_LABEL=TYPE_LABEL, ACTION_LABEL=ACTION_LABEL,
    )
