"""
Domain logic shared across the API blueprints: the workflow state machine,
access + quota + asset helpers, request serialization and notifications.
"""
import base64
import binascii
import threading

from flask import jsonify, g

from . import audit, db
from .mailer import send_email

# Workflow states and the role that acts on each. EVERY request type follows the
# same path:  HR -> Director -> AA -> IT (process) -> Requestor (appointment) -> EUSS
STATE_ROLE = {
    "pending_hr": "hr",
    "pending_director": "director",
    "pending_aa": "aa",
    "pending_it": "it_admin",
    "pending_appointment": "requestor",
    "pending_euss": "euss",
}
APPROVAL_NEXT = {
    "pending_hr": "pending_director",
    "pending_director": "pending_aa",
    "pending_aa": "pending_it",
}
VALID_ROLES = {"employee", "requestor", "hr", "director", "aa", "it_admin", "euss", "admin"}
KIND_OF = {"laptop": "Laptop", "account": "Account"}
STAFF_ROLES = ("hr", "director", "aa", "it_admin", "euss", "admin")

# The HR-system webhook (CHANGE 008) raises requests as this account. It exists only so a
# request has a requestor to show; it has no Keycloak login, no sign-in and no console access.
SYSTEM_USERNAME = "hr-system"
ACCESS_REVIEW_SYSTEM_USERNAME = "access-review-system"
SYSTEM_ROLE = "system"


def system_actor(username=SYSTEM_USERNAME):
    """A non-login automation account, or None if it has not been seeded."""
    return db.query("SELECT id, username, full_name, role FROM employees WHERE username=%s AND role=%s",
                    (username, SYSTEM_ROLE), fetch="one")


def access_review_system_actor():
    """The automation identity that owns returns initiated by an access review."""
    return system_actor(ACCESS_REVIEW_SYSTEM_USERNAME)


def is_system_actor_id(emp_id):
    """True when `emp_id` is the automation account (a request it raised belongs to the
    whole Requestor role, since nobody can sign in as it to book the appointment)."""
    return db.query("SELECT 1 FROM employees WHERE id=%s AND role=%s", (emp_id, SYSTEM_ROLE), fetch="one") is not None


# --------------------------------------------------------------------------- #
# Requests + events
# --------------------------------------------------------------------------- #
def get_request(rid):
    return db.query("SELECT * FROM requests WHERE id = %s", (rid,), fetch="one")


def set_status(rid, status):
    db.query("UPDATE requests SET status = %s, updated_at = now() WHERE id = %s", (status, rid), fetch=None)


def add_event(rid, actor_id, action, note=None, notify_incoming=True, notify_target=True, background=False):
    db.query("INSERT INTO request_events (request_id, actor_id, action, note) VALUES (%s,%s,%s,%s)",
             (rid, actor_id, action, note), fetch=None)
    r = get_joined(rid)
    if r:   # mirror every workflow step into the tamper-evident audit trail
        audit.record("workflow", f"request_{action}", outcome="denied" if action == "rejected" else "success",
                     target_type="request", target_id=rid,
                     target_label=f"#{rid} {r['type']} for {r['target_username']}",
                     details={"type": r["type"], "status": r["status"], "note": note,
                              "resource": _resource_desc(r), "target_employee": r["target_username"],
                              "on_behalf": r["on_behalf"]})
    # notify next approver, requestor/target, and the actor. notify_incoming=False skips the
    # "action required" mail when the next step is done by the system at once (CHANGE 011).
    # background=True sends the e-mails on a worker thread: real SMTP can take seconds per
    # message, which must not hold up the person's click.
    if background:
        threading.Thread(target=_notify_quietly, args=(rid, action, note, actor_id, notify_incoming, notify_target),
                         daemon=True).start()
    else:
        notify(rid, action, note, actor_id, incoming=notify_incoming, target=notify_target)


def _notify_quietly(*args):
    try:
        notify(*args[:4], incoming=args[4], target=args[5])
    except Exception as e:  # noqa: BLE001 — e-mail is best-effort
        print(f"[email] notification for request #{args[0]} failed: {e}", flush=True)


def _resource_desc(r):
    if r["type"] == "onboard":
        return f"onboarding into {r['new_department_name'] or '?'}"
    if r["type"] == "shift":
        return f"department transfer {r['source_department_name'] or '?'} -> {r['new_department_name'] or '?'}"
    return r["service_name"] or "(resource)"


STATUS_TEXT = {
    "pending_hr": "awaiting HR approval",
    "pending_director": "awaiting Director approval",
    "pending_aa": "awaiting Approving Authority approval",
    "pending_it": "awaiting IT processing",
    "pending_appointment": "awaiting the requestor to book an appointment",
    "pending_euss": "awaiting EUSS",
    "completed": "completed",
    "rejected": "rejected",
}


def _emails_for_role(role):
    return db.query("SELECT full_name, email FROM employees WHERE role=%s AND status='active' AND email IS NOT NULL",
                    (role,))


def _email_of(emp_id):
    row = db.query("SELECT full_name, email FROM employees WHERE id=%s", (emp_id,), fetch="one")
    return row if row and row["email"] else None


def notify(rid, action, note=None, actor_id=None, incoming=True, target=True):
    """Send the notifications around a request event:

      1. INCOMING  — whoever the request has just landed on ("action required")
      2. OUTCOME   — the requestor and the target employee (progress)
      3. DONE      — a confirmation to the person who just acted
    """
    r = get_joined(rid)
    if not r:
        return
    sent = set()

    def send(person, subject, body):
        if not person or not person.get("email"):
            return
        key = (person["email"], subject)
        if key in sent:
            return
        sent.add(key)
        send_email(person["email"], subject, body)

    header = (f"Request #{rid} — {r['type']} for {r['target_name']}\n"
              f"Resource: {_resource_desc(r)}\n"
              f"Raised by: {r['requestor_name']}"
              f"{' (on behalf)' if r['on_behalf'] else ''}")
    if r.get("justification"):
        header += f"\nReason given: {r['justification']}"
    status_line = f"Status: {STATUS_TEXT.get(r['status'], r['status'])}"

    # 1) The request has just arrived in someone's queue.
    role = STATE_ROLE.get(r["status"]) if incoming else None
    if role:
        if role == "requestor":
            # A request raised by the HR-system account has no mailbox: every Requestor is told.
            recipients = (_emails_for_role("requestor") if is_system_actor_id(r["requestor_id"])
                          else [_email_of(r["requestor_id"])])
        else:
            recipients = _emails_for_role(role)
        for person in recipients:
            send(person, f"[ServiceJourney] ACTION REQUIRED — request #{rid} is waiting on you",
                 f"{header}\n\n{status_line}\n\nA request now needs your action. "
                 f"Open the console to review and act on it.")

    # 2) Progress update for the requestor and the employee it concerns.
    for eid in ((r["requestor_id"], r["target_employee_id"]) if target else (r["requestor_id"],)):
        send(_email_of(eid), f"[ServiceJourney] Request #{rid} — {action}",
             f"{header}\n\nUpdate: {action}\n{status_line}" + (f"\nNote: {note}" if note else ""))

    # 3) Confirmation to whoever just acted.
    if actor_id and action in ("approved", "rejected", "processed", "provisioned",
                               "retrieved", "appointment_booked", "submitted"):
        send(_email_of(actor_id), f"[ServiceJourney] You {action} request #{rid}",
             f"{header}\n\nYou {action} this request.\n{status_line}"
             + (f"\nNote: {note}" if note else ""))


# Supporting documents must be PDFs. The %PDF- magic header is the ACTUAL control:
# a filename or a client-supplied MIME type is trivially forged, so neither is
# trusted on its own. Everything is capped so a single upload cannot exhaust storage.
PDF_MAGIC = b"%PDF-"
MAX_DOC_BYTES = 5 * 1024 * 1024   # 5 MB per document (before base64 inflation)
MAX_DOCUMENTS = 10


def validate_documents(documents):
    """Authoritative upload check. Returns an error string, or None when every
    document is a well-formed PDF within the size limits. Called before a request
    is created, so a bad attachment rejects the whole submission untouched."""
    documents = documents or []
    if len(documents) > MAX_DOCUMENTS:
        return f"Too many documents (max {MAX_DOCUMENTS})."
    for doc in documents:
        name = (doc.get("filename") or "").strip()
        if not name.lower().endswith(".pdf"):
            return f"'{name or 'file'}' is not a PDF. Only PDF files are allowed."
        try:
            raw = base64.b64decode(doc.get("content_base64") or "")
        except (binascii.Error, ValueError):
            return f"'{name}' could not be decoded."
        if not raw:
            return f"'{name}' is empty."
        if len(raw) > MAX_DOC_BYTES:
            return f"'{name}' exceeds the {MAX_DOC_BYTES // (1024 * 1024)} MB limit."
        if not raw.startswith(PDF_MAGIC):
            return f"'{name}' is not a valid PDF (failed content check)."
    return None


def attach_documents(rid, documents):
    for doc in (documents or []):
        filename = (doc.get("filename") or "document").strip()
        content = doc.get("content_base64") or ""
        if content:
            db.query("INSERT INTO request_documents (request_id, filename, content_base64) VALUES (%s,%s,%s)",
                     (rid, filename, content), fetch=None)


# --------------------------------------------------------------------------- #
# Access
# --------------------------------------------------------------------------- #
def has_access(emp_id, service_id):
    """Does the employee HOLD this access at all (suspended or not)?"""
    return db.query("SELECT 1 FROM employee_access WHERE employee_id=%s AND service_id=%s",
                    (emp_id, service_id), fetch="one") is not None


def access_row(emp_id, service_id):
    return db.query("SELECT * FROM employee_access WHERE employee_id=%s AND service_id=%s",
                    (emp_id, service_id), fetch="one")


def access_state(emp_id, service_id):
    """None when the employee may use this service, else the reason.

    The laptop and account are revoked together, so an employee either holds a
    usable pair or holds nothing — there is no in-between state.
    """
    return None if access_row(emp_id, service_id) else "revoked"


def revoke_with_dependents(emp_id, service_id):
    """Revoke a resource, honouring the PRIVILEGE HIERARCHY.

    The laptop is the higher privilege and depends on the account (the lower one):
      * revoke the laptop  -> the account REMAINS (you can hold an account alone)
      * revoke the account -> the laptop goes too (a laptop needs an account)
    Returns the service ids that were actually revoked.
    """
    revoke_access(emp_id, service_id)
    removed = [service_id]
    svc = db.query("SELECT resource_type FROM services WHERE id=%s", (service_id,), fetch="one")
    if svc and svc["resource_type"] == "account":
        laptop = service_by_type("laptop")
        if laptop and has_access(emp_id, laptop["id"]):
            revoke_access(emp_id, laptop["id"])
            removed.append(laptop["id"])
    return removed


def _audit_entitlement(action, emp_id, service_id):
    who = db.query("SELECT username FROM employees WHERE id=%s", (emp_id,), fetch="one")
    svc = db.query("SELECT key, name FROM services WHERE id=%s", (service_id,), fetch="one")
    audit.record("access", action, target_type="employee", target_id=emp_id,
                 target_label=who and who["username"],
                 details={"service": svc and svc["name"], "service_key": svc and svc["key"]})


def grant_access(emp_id, service_id):
    row = db.query("""INSERT INTO employee_access (employee_id, service_id) VALUES (%s,%s)
                      ON CONFLICT DO NOTHING RETURNING employee_id""", (emp_id, service_id), fetch="one")
    if row:   # only a real change is an entitlement event
        _audit_entitlement("entitlement_granted", emp_id, service_id)


def revoke_access(emp_id, service_id):
    row = db.query("DELETE FROM employee_access WHERE employee_id=%s AND service_id=%s RETURNING employee_id",
                   (emp_id, service_id), fetch="one")
    if row:
        _audit_entitlement("entitlement_revoked", emp_id, service_id)


def service_by_type(rtype):
    return db.query("SELECT * FROM services WHERE resource_type=%s ORDER BY id LIMIT 1", (rtype,), fetch="one")


def department_effective_services(dept_id):
    """The department's template services, including the account a laptop carries."""
    ids = [r["service_id"] for r in
           db.query("SELECT service_id FROM department_access WHERE department_id=%s", (dept_id,))]
    laptop, account = service_by_type("laptop"), service_by_type("account")
    if laptop and account and laptop["id"] in ids and account["id"] not in ids:
        ids = ids + [account["id"]]
    return ids


def apply_department(target, dept_id):
    """Grant the department's template services (+ coupling account for a laptop)."""
    svc_ids = department_effective_services(dept_id)
    for sid in svc_ids:
        grant_access(target, sid)
    return svc_ids


def service_names(svc_ids):
    if not svc_ids:
        return "no services"
    return ", ".join(s["name"] for s in db.query("SELECT name FROM services WHERE id = ANY(%s) ORDER BY id", (svc_ids,)))


# --------------------------------------------------------------------------- #
# Quotas
# --------------------------------------------------------------------------- #
def quota_used(service_id):
    return db.query("SELECT COUNT(*) AS c FROM employee_access WHERE service_id=%s", (service_id,), fetch="one")["c"]


def quota_allowed(service_id):
    r = db.query("SELECT allowed FROM quotas WHERE service_id=%s", (service_id,), fetch="one")
    return r["allowed"] if r else 0


def dept_quota_allowed(dept_id, service_id):
    r = db.query("SELECT allowed FROM department_quotas WHERE department_id=%s AND service_id=%s",
                 (dept_id, service_id), fetch="one")
    return r["allowed"] if r else 0


def dept_quota_used(dept_id, service_id):
    """How many employees IN THAT DEPARTMENT currently hold the resource."""
    return db.query("""SELECT COUNT(*) AS c FROM employee_access ea
                       JOIN employees e ON e.id = ea.employee_id
                       WHERE e.department_id=%s AND ea.service_id=%s""",
                    (dept_id, service_id), fetch="one")["c"]


def quota_overages(target_id, dept_id, service_ids):
    """Which caps granting these resources to this employee would exceed.

    Used by grant, onboard AND shift so every path enforces quota identically.
      * org-wide  — only counts resources the employee does not already hold
                    (a shift that keeps the same resource is net-zero)
      * department — the employee is not yet counted in the destination department,
                    so a full department has no room for them
    """
    over = []
    dept = db.query("SELECT name FROM departments WHERE id=%s", (dept_id,), fetch="one") if dept_id else None
    for sid in service_ids:
        svc = db.query("SELECT name FROM services WHERE id=%s", (sid,), fetch="one")
        if not svc:
            continue
        if not has_access(target_id, sid):
            used, allowed = quota_used(sid), quota_allowed(sid)
            if used >= allowed:
                over.append(f"{svc['name']} org-wide ({used}/{allowed})")
        if dept:
            dused, dallowed = dept_quota_used(dept_id, sid), dept_quota_allowed(dept_id, sid)
            if dused >= dallowed:
                over.append(f"{svc['name']} in {dept['name']} ({dused}/{dallowed})")
    return over


def set_department_quota(dept_id, service_id, allowed):
    db.query("""INSERT INTO department_quotas (department_id, service_id, allowed)
                VALUES (%s,%s,%s)
                ON CONFLICT (department_id, service_id) DO UPDATE SET allowed = EXCLUDED.allowed""",
             (dept_id, service_id, allowed), fetch=None)


# --------------------------------------------------------------------------- #
# Assets
# --------------------------------------------------------------------------- #
def add_asset_event(asset_id, emp_id, action, request_id):
    db.query("INSERT INTO asset_events (asset_id, employee_id, action, request_id) VALUES (%s,%s,%s,%s)",
             (asset_id, emp_id, action, request_id), fetch=None)
    a = db.query("SELECT asset_tag, kind FROM assets WHERE id=%s", (asset_id,), fetch="one")
    who = db.query("SELECT username FROM employees WHERE id=%s", (emp_id,), fetch="one")
    audit.record("asset", f"asset_{action}", target_type="asset", target_id=a and a["asset_tag"],
                 target_label=f"{a['kind']} {a['asset_tag']}" if a else None,
                 details={"employee": who and who["username"], "request_id": request_id})


def employee_asset_kinds_needed(emp_id):
    rows = db.query("""SELECT DISTINCT s.resource_type FROM employee_access ea
                       JOIN services s ON s.id = ea.service_id WHERE ea.employee_id=%s""", (emp_id,))
    return {KIND_OF[r["resource_type"]] for r in rows if r["resource_type"] in KIND_OF}


def held_assets(emp_id):
    return db.query("SELECT * FROM assets WHERE assigned_to=%s AND status='assigned'", (emp_id,))


def assign_asset(asset, emp_id, request_id):
    db.query("UPDATE assets SET status='assigned', assigned_to=%s WHERE id=%s", (emp_id, asset["id"]), fetch=None)
    add_asset_event(asset["id"], emp_id, "provisioned", request_id)


def release_asset(asset, emp_id, request_id):
    db.query("UPDATE assets SET status='available', assigned_to=NULL WHERE id=%s", (asset["id"],), fetch=None)
    add_asset_event(asset["id"], emp_id, "retrieved", request_id)


def release_unneeded_assets(emp_id, request_id):
    """Return any assigned asset whose kind the employee no longer has access to."""
    need = employee_asset_kinds_needed(emp_id)
    for a in held_assets(emp_id):
        if a["kind"] not in need:
            release_asset(a, emp_id, request_id)


# --------------------------------------------------------------------------- #
# Serialization
# --------------------------------------------------------------------------- #
REQ_SELECT = """
    SELECT r.*, rq.full_name AS requestor_name,
           te.full_name AS target_name, te.username AS target_username,
           td.name AS target_department_name,
           nd.name AS new_department_name,
           sd.name AS source_department_name,
           s.name AS service_name, s.key AS service_key,
           a.asset_tag AS asset_tag
    FROM requests r
    JOIN employees rq ON rq.id = r.requestor_id
    JOIN employees te ON te.id = r.target_employee_id
    LEFT JOIN departments td ON td.id = te.department_id
    LEFT JOIN departments nd ON nd.id = r.target_department_id
    LEFT JOIN departments sd ON sd.id = r.source_department_id
    LEFT JOIN services  s  ON s.id  = r.service_id
    LEFT JOIN assets a ON a.id = r.asset_id
"""


def get_joined(rid):
    return db.query(REQ_SELECT + " WHERE r.id = %s", (rid,), fetch="one")


def request_dict(row, with_events=False):
    d = {
        "id": row["id"], "type": row["type"], "status": row["status"],
        "requestor_id": row["requestor_id"], "requestor_name": row["requestor_name"],
        "target_employee_id": row["target_employee_id"], "target_name": row["target_name"],
        "target_username": row["target_username"],
        "service_id": row["service_id"], "service_name": row["service_name"], "service_key": row["service_key"],
        "target_department_name": row["target_department_name"],
        "target_department_id": row["target_department_id"], "new_department_name": row["new_department_name"],
        "source_department_name": row["source_department_name"],
        "on_behalf": row["on_behalf"],
        "justification": row["justification"], "reject_reason": row["reject_reason"],
        "appointment_at": row["appointment_at"].isoformat() if row["appointment_at"] else None,
        "asset_tag": row["asset_tag"],
        "created_at": row["created_at"].isoformat(), "updated_at": row["updated_at"].isoformat(),
    }
    if with_events:
        evs = db.query(
            """SELECT e.action, e.note, e.created_at, em.full_name AS actor_name, em.role AS actor_role
               FROM request_events e LEFT JOIN employees em ON em.id = e.actor_id
               WHERE e.request_id = %s ORDER BY e.created_at, e.id""", (row["id"],))
        d["events"] = [{
            "action": e["action"], "note": e["note"], "created_at": e["created_at"].isoformat(),
            "actor_name": e["actor_name"], "actor_role": e["actor_role"],
        } for e in evs]
        docs = db.query("SELECT id, filename FROM request_documents WHERE request_id=%s ORDER BY id", (row["id"],))
        d["documents"] = [{"id": x["id"], "filename": x["filename"]} for x in docs]
    return d


def load_stage(rid, expected_status):
    """Fetch a request, verifying it's at the expected stage and the caller holds
    the acting role. Returns (request, None) or (None, (json, code))."""
    req = get_request(rid)
    if not req:
        return None, (jsonify(error="Request not found"), 404)
    if req["status"] != expected_status:
        return None, (jsonify(error=f"Request is not at stage '{expected_status}' (it is '{req['status']}')"), 409)
    needed = STATE_ROLE.get(expected_status)
    if g.user["role"] != needed:
        return None, (jsonify(error=f"This action requires role '{needed}'"), 403)
    if req["target_employee_id"] == g.user["id"]:
        audit.record("workflow", "request_self_approval_blocked", outcome="denied",
                     target_type="request", target_id=rid,
                     target_label=f"#{rid} {req['type']}", details={"actor_id": g.user["id"]})
        return None, (jsonify(error="You can't act on a request that is about you (separation of duties)."), 403)
    return req, None


QR_SELECT = """
    SELECT qr.*, s.name AS service_name,
           rb.full_name AS requested_by_name, db.full_name AS decided_by_name
    FROM quota_requests qr
    JOIN services s ON s.id = qr.service_id
    JOIN employees rb ON rb.id = qr.requested_by
    LEFT JOIN employees db ON db.id = qr.decided_by
"""


def quota_request_dict(r):
    return {
        "id": r["id"], "service_id": r["service_id"], "service_name": r["service_name"],
        "requested_by_name": r["requested_by_name"], "requested_by": r["requested_by"],
        "current_allowed": r["current_allowed"], "new_allowed": r["new_allowed"],
        "reason": r["reason"], "status": r["status"],
        "decided_by_name": r["decided_by_name"], "decide_note": r["decide_note"],
        "created_at": r["created_at"].isoformat(), "updated_at": r["updated_at"].isoformat(),
    }
