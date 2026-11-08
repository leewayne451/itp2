"""The request lifecycle: creation, listing, and every workflow transition."""
from flask import Blueprint, request, jsonify, g

from .. import audit, db, rotation, sync
from ..auth import require_auth, require_role
from ..core import (
    STATE_ROLE, APPROVAL_NEXT, STAFF_ROLES, KIND_OF,
    get_request, get_joined, set_status, add_event, attach_documents, validate_documents,
    request_dict, load_stage, has_access, grant_access, revoke_access,
    service_by_type, service_names, department_effective_services, quota_overages,
    revoke_with_dependents, is_system_actor_id,
    employee_asset_kinds_needed, held_assets, assign_asset, release_unneeded_assets,
)

bp = Blueprint("requests_api", __name__)


# --------------------------------------------------------------------------- #
# Creation
# --------------------------------------------------------------------------- #
@bp.post("/api/requests")
@require_role("requestor")
def create_request():
    """Raise one of the four workflows — they ALL share the same operational flow:
       HR -> Director -> AA -> IT (approve/execute) -> requestor books appointment -> EUSS

      onboard — place a department-less employee into a department + its services
      grant   — give an existing employee one more service
      return  — offboard: revoke the laptop+account and take the assets back
      shift   — move an existing employee to another department
    """
    body = request.get_json(silent=True) or {}
    rtype = body.get("type")
    target_id = body.get("target_employee_id")
    justification = (body.get("justification") or "").strip()
    on_behalf = bool(body.get("on_behalf"))
    documents = body.get("documents") or []
    if rtype not in ("grant", "return", "shift", "onboard"):
        return jsonify(error="type must be grant, return, shift or onboard"), 400
    docs_error = validate_documents(documents)
    if docs_error:
        return jsonify(error=docs_error), 400
    if not target_id:
        return jsonify(error="target_employee_id is required"), 400
    target = db.query("SELECT * FROM employees WHERE id=%s", (target_id,), fetch="one")
    if not target:
        return jsonify(error="Target employee not found"), 404
    if target["role"] != "employee":
        return jsonify(error="Requests can only target ordinary employees"), 400

    if rtype == "onboard":
        return _create_onboard(target, on_behalf, justification, documents, body)
    if rtype == "shift":
        return _create_shift(target, on_behalf, justification, documents, body)
    return _create_grant_return(rtype, target, on_behalf, justification, documents, body)


def _create_onboard(target, on_behalf, justification, documents, body):
    if target["department_id"] is not None:
        return jsonify(error="Onboarding is only for employees not yet in a department; "
                             "use an access request for existing employees."), 409
    dept_id = body.get("department_id")
    if not dept_id:
        return jsonify(error="department_id (onboarding department) is required"), 400
    if not db.query("SELECT 1 FROM departments WHERE id=%s", (dept_id,), fetch="one"):
        return jsonify(error="Department not found"), 404
    if db.query("SELECT 1 FROM requests WHERE target_employee_id=%s AND type='onboard' "
                "AND status NOT IN ('completed','rejected')", (target["id"],), fetch="one"):
        return jsonify(error="An onboarding for this employee is already in progress"), 409
    row = db.query(
        """INSERT INTO requests (type, requestor_id, target_employee_id, target_department_id,
                                 on_behalf, justification, status)
           VALUES ('onboard',%s,%s,%s,%s,%s,'pending_hr') RETURNING id""",
        (g.user["id"], target["id"], dept_id, on_behalf, justification), fetch="one")
    attach_documents(row["id"], documents)
    add_event(row["id"], g.user["id"], "submitted", "Onboarding requested")
    return jsonify(request_dict(get_joined(row["id"]))), 201


def _create_shift(target, on_behalf, justification, documents, body):
    if target["department_id"] is None:
        return jsonify(error="This employee has no department yet — onboard them first."), 409
    dept_id = body.get("department_id")
    if not dept_id:
        return jsonify(error="department_id (destination) is required"), 400
    if not db.query("SELECT 1 FROM departments WHERE id=%s", (dept_id,), fetch="one"):
        return jsonify(error="Department not found"), 404
    if target["department_id"] == dept_id:
        return jsonify(error="Employee is already in that department"), 409
    if db.query("SELECT 1 FROM requests WHERE target_employee_id=%s AND type='shift' "
                "AND status NOT IN ('completed','rejected')", (target["id"],), fetch="one"):
        return jsonify(error="A shift for this employee is already in progress"), 409
    # MJR (CHANGE 006): a shift raised from a Director's "rotate" decision is linked to it.
    rotation_id = body.get("rotation_decision_id")
    if rotation_id:
        link_error = rotation.validate_link(rotation_id, target["id"])
        if link_error:
            return jsonify(error=link_error), 409
    row = db.query(
        """INSERT INTO requests (type, requestor_id, target_employee_id, target_department_id,
                                 source_department_id, on_behalf, justification, status)
           VALUES ('shift',%s,%s,%s,%s,%s,%s,'pending_hr') RETURNING id""",
        (g.user["id"], target["id"], dept_id, target["department_id"], on_behalf, justification), fetch="one")
    db.query("UPDATE employees SET status='suspended' WHERE id=%s", (target["id"],), fetch=None)
    sync.push(target["id"], end_sessions=True)   # Keycloak: account disabled, signed out everywhere
    attach_documents(row["id"], documents)
    if rotation_id:
        rotation.link_shift(rotation_id, row["id"])
    add_event(row["id"], g.user["id"], "submitted",
              "Department shift requested; current services suspended pending transfer"
              + (" (Mandatory Job Rotation, confirmed by Director)" if rotation_id else ""))
    return jsonify(request_dict(get_joined(row["id"]))), 201


def _create_grant_return(rtype, target, on_behalf, justification, documents, body):
    if target["department_id"] is None:
        return jsonify(error="This employee is not onboarded yet — onboard them into a department first."), 409
    service_id = body.get("service_id")
    if not service_id:
        return jsonify(error="service_id is required"), 400
    if not db.query("SELECT 1 FROM services WHERE id=%s", (service_id,), fetch="one"):
        return jsonify(error="Service not found"), 404
    already = has_access(target["id"], service_id)
    if rtype == "grant" and already:
        return jsonify(error="Target already has access to this service"), 409
    if rtype == "return" and not already:
        return jsonify(error="Target does not have access to this service"), 409
    if db.query("SELECT 1 FROM requests WHERE target_employee_id=%s AND service_id=%s AND type=%s "
                "AND status NOT IN ('completed','rejected')", (target["id"], service_id, rtype), fetch="one"):
        return jsonify(error="An open request of this type already exists for that employee/service"), 409
    row = db.query(
        """INSERT INTO requests (type, requestor_id, target_employee_id, service_id, on_behalf, justification, status)
           VALUES (%s,%s,%s,%s,%s,%s,'pending_hr') RETURNING id""",
        (rtype, g.user["id"], target["id"], service_id, on_behalf, justification), fetch="one")
    attach_documents(row["id"], documents)
    add_event(row["id"], g.user["id"], "submitted",
              "Return (offboard) request submitted" if rtype == "return" else "Access request submitted")
    return jsonify(request_dict(get_joined(row["id"]))), 201


# --------------------------------------------------------------------------- #
# Listing
# --------------------------------------------------------------------------- #
@bp.get("/api/requests/mine")
@require_auth
def my_requests():
    from ..core import REQ_SELECT
    rows = db.query(REQ_SELECT + " WHERE r.requestor_id = %s ORDER BY r.created_at DESC", (g.user["id"],))
    return jsonify([request_dict(r) for r in rows])


@bp.get("/api/requests/queue")
@require_auth
def my_queue():
    from ..core import REQ_SELECT
    role = g.user["role"]
    statuses = [s for s, r in STATE_ROLE.items() if r == role]
    if not statuses:
        return jsonify([])
    rows = db.query(REQ_SELECT + " WHERE r.status = ANY(%s) ORDER BY r.created_at", (statuses,))
    if role == "requestor":  # the appointment queue belongs to the owning requestor
        # ...except requests the HR-system account raised: nobody can sign in as it, so any
        # Requestor may pick those up and book the appointment.
        rows = [r for r in rows if r["requestor_id"] == g.user["id"] or is_system_actor_id(r["requestor_id"])]
    return jsonify([request_dict(r) for r in rows])


@bp.get("/api/requests/history")
@require_auth
def my_history():
    from ..core import REQ_SELECT
    uid = g.user["id"]
    rows = db.query(REQ_SELECT + " WHERE EXISTS (SELECT 1 FROM request_events e"
                    " WHERE e.request_id = r.id AND e.actor_id = %s) ORDER BY r.updated_at DESC", (uid,))
    out = []
    for r in rows:
        d = request_dict(r)
        ev = db.query("SELECT action, created_at FROM request_events WHERE request_id=%s AND actor_id=%s"
                      " ORDER BY created_at DESC, id DESC LIMIT 1", (r["id"], uid), fetch="one")
        d["my_action"] = ev["action"] if ev else None
        d["my_action_at"] = ev["created_at"].isoformat() if ev and ev["created_at"] else None
        out.append(d)
    return jsonify(out)


@bp.get("/api/requests")
@require_role("admin")
def all_requests():
    from ..core import REQ_SELECT
    return jsonify([request_dict(r) for r in db.query(REQ_SELECT + " ORDER BY r.created_at DESC")])


def _may_view(requestor_id):
    """Staff see every request; a Requestor sees their own and the HR system's."""
    if g.user["role"] in STAFF_ROLES or requestor_id == g.user["id"]:
        return True
    return g.user["role"] == "requestor" and is_system_actor_id(requestor_id)


@bp.get("/api/requests/<int:rid>")
@require_auth
def request_detail(rid):
    row = get_joined(rid)
    if not row:
        return jsonify(error="Request not found"), 404
    if not _may_view(row["requestor_id"]):
        return jsonify(error="Forbidden"), 403
    return jsonify(request_dict(row, with_events=True))


@bp.get("/api/documents/<int:doc_id>")
@require_auth
def get_document(doc_id):
    doc = db.query("""SELECT d.filename, d.content_base64, r.requestor_id
                      FROM request_documents d JOIN requests r ON r.id = d.request_id
                      WHERE d.id=%s""", (doc_id,), fetch="one")
    if not doc:
        return jsonify(error="Document not found"), 404
    if not _may_view(doc["requestor_id"]):
        return jsonify(error="Forbidden"), 403
    return jsonify(filename=doc["filename"], content_base64=doc["content_base64"])


# --------------------------------------------------------------------------- #
# Transitions
# --------------------------------------------------------------------------- #
@bp.post("/api/requests/<int:rid>/approve")
@require_auth
def approve(rid):
    req = get_request(rid)
    if not req:
        return jsonify(error="Request not found"), 404
    status = req["status"]
    if status not in APPROVAL_NEXT:
        return jsonify(error=f"Cannot approve at stage '{status}'"), 409
    if g.user["role"] != STATE_ROLE[status]:
        return jsonify(error=f"This step requires role '{STATE_ROLE[status]}'"), 403
    if req["target_employee_id"] == g.user["id"]:
        audit.record("workflow", "request_self_approval_blocked", outcome="denied",
                     target_type="request", target_id=rid,
                     target_label=f"#{rid} {req['type']}", details={"actor_id": g.user["id"]})
        return jsonify(error="You can't approve a request that is about you (separation of duties)."), 403
    set_status(rid, APPROVAL_NEXT[status])
    add_event(rid, g.user["id"], "approved", f"Approved by {g.user['role']}")
    return jsonify(request_dict(get_joined(rid)))


@bp.post("/api/requests/<int:rid>/reject")
@require_auth
def reject(rid):
    req = get_request(rid)
    if not req:
        return jsonify(error="Request not found"), 404
    status = req["status"]
    if status not in ("pending_hr", "pending_director", "pending_aa", "pending_it"):
        return jsonify(error=f"Cannot reject at stage '{status}'"), 409
    if g.user["role"] != STATE_ROLE[status]:
        return jsonify(error="Not your stage to reject"), 403
    if req["target_employee_id"] == g.user["id"]:
        audit.record("workflow", "request_self_approval_blocked", outcome="denied",
                     target_type="request", target_id=rid,
                     target_label=f"#{rid} {req['type']}", details={"actor_id": g.user["id"]})
        return jsonify(error="You can't reject a request that is about you (separation of duties)."), 403
    reason = (request.get_json(silent=True) or {}).get("reason", "").strip() or "No reason given"
    db.query("UPDATE requests SET status='rejected', reject_reason=%s, updated_at=now() WHERE id=%s", (reason, rid), fetch=None)
    if req["type"] == "shift":  # a rejected shift lifts the suspension
        db.query("UPDATE employees SET status='active' WHERE id=%s AND status='suspended'",
                 (req["target_employee_id"],), fetch=None)
        sync.push(req["target_employee_id"])     # Keycloak: account enabled again
    add_event(rid, g.user["id"], "rejected", reason)
    return jsonify(request_dict(get_joined(rid)))


@bp.post("/api/requests/<int:rid>/process")
@require_auth
def process_request(rid):
    """IT executes the approved request. grant/onboard/shift change access here;
    return defers its revoke to the EUSS step. All advance to the appointment."""
    req, err = load_stage(rid, "pending_it")
    if err:
        return err
    if req["type"] == "shift":
        return _process_shift(req)
    if req["type"] == "onboard":
        return _process_onboard(req)
    if req["type"] == "return":
        set_status(rid, "pending_appointment")
        add_event(rid, g.user["id"], "processed", "Offboarding approved; awaiting return appointment")
        return jsonify(request_dict(get_joined(rid)))
    return _process_grant(req)


def _process_grant(req):
    target = req["target_employee_id"]
    svc = db.query("SELECT * FROM services WHERE id=%s", (req["service_id"],), fetch="one")
    account = service_by_type("account")
    to_grant = []
    if not has_access(target, svc["id"]):
        to_grant.append(svc)
    if svc["resource_type"] == "laptop" and account and not has_access(target, account["id"]):
        to_grant.append(account)
    # Both caps apply: the organisation-wide quota and the target's department quota.
    emp = db.query("SELECT department_id FROM employees WHERE id=%s", (target,), fetch="one")
    over = quota_overages(target, emp["department_id"], [s["id"] for s in to_grant])
    override_reason = (request.get_json(silent=True) or {}).get("override_reason")
    if over and not override_reason:
        return jsonify(requires_override=True, warning="Quota exceeded for: " + ", ".join(over)), 409
    for s in to_grant:
        grant_access(target, s["id"])
    set_status(req["id"], "pending_appointment")
    note = "Access provisioned" + (" (laptop + account)" if svc["resource_type"] == "laptop" else "")
    if over:
        note += f" — OVER-QUOTA OVERRIDE by IT: {override_reason} [{', '.join(over)}]"
        audit.record("quota", "quota_override", target_type="request", target_id=req["id"],
                     target_label=f"#{req['id']} {req['type']}",
                     details={"exceeded": over, "justification": override_reason})
    add_event(req["id"], g.user["id"], "processed", note)
    return jsonify(request_dict(get_joined(req["id"])))


def _process_into_department(req, verb):
    """Shared executor for onboard and shift: both place the employee in a
    department and grant its services, and BOTH are quota-checked first."""
    target = req["target_employee_id"]
    new_dept = req["target_department_id"]
    dept = db.query("SELECT name FROM departments WHERE id=%s", (new_dept,), fetch="one")
    svc_ids = department_effective_services(new_dept)

    # Quota is checked BEFORE anything is mutated, so a blocked request is untouched.
    over = quota_overages(target, new_dept, svc_ids)
    override_reason = (request.get_json(silent=True) or {}).get("override_reason")
    if over and not override_reason:
        return jsonify(requires_override=True, warning="Quota exceeded for: " + ", ".join(over)), 409

    if verb == "shift":
        # Revoke one by one so every removed entitlement is individually audited.
        for row in db.query("SELECT service_id FROM employee_access WHERE employee_id=%s", (target,)):
            revoke_access(target, row["service_id"])
    # department_since restarts the Mandatory Job Rotation clock (CHANGE 006).
    db.query("UPDATE employees SET department_id=%s, status='active', department_since=CURRENT_DATE, "
             "rotation_alerted_at=NULL WHERE id=%s", (new_dept, target), fetch=None)
    sync.push(target)                            # Keycloak: account enabled again
    for sid in svc_ids:
        grant_access(target, sid)

    set_status(req["id"], "pending_appointment")
    note = (f"Transferred to {dept['name']}; services reset to department template ({service_names(svc_ids)})"
            if verb == "shift" else
            f"Onboarded into {dept['name']}; services set to department template ({service_names(svc_ids)})")
    if over:
        note += f" — OVER-QUOTA OVERRIDE by IT: {override_reason} [{', '.join(over)}]"
        audit.record("quota", "quota_override", target_type="request", target_id=req["id"],
                     target_label=f"#{req['id']} {req['type']}",
                     details={"exceeded": over, "justification": override_reason})
    add_event(req["id"], g.user["id"], "processed", note)
    return jsonify(request_dict(get_joined(req["id"])))


def _process_shift(req):
    return _process_into_department(req, "shift")


def _process_onboard(req):
    return _process_into_department(req, "onboard")


@bp.post("/api/requests/<int:rid>/appointment")
@require_auth
def book_appointment(rid):
    req, err = load_stage(rid, "pending_appointment")
    if err:
        return err
    if req["requestor_id"] != g.user["id"] and not is_system_actor_id(req["requestor_id"]):
        return jsonify(error="Only the requestor who raised this may book the appointment"), 403
    when = (request.get_json(silent=True) or {}).get("appointment_at")
    if not when:
        return jsonify(error="appointment_at is required"), 400
    db.query("UPDATE requests SET appointment_at=%s, status='pending_euss', updated_at=now() WHERE id=%s",
             (when, rid), fetch=None)
    add_event(rid, g.user["id"], "appointment_booked", f"Appointment booked for {when}")
    return jsonify(request_dict(get_joined(rid)))


def _validate_tags(target, body):
    """Resolve the asset tags for every kind the employee still needs.

    A tag is REQUIRED per kind and must be an existing, AVAILABLE asset of exactly
    that kind. Returns (chosen_assets, None) or (None, error_response).
    """
    plan = provision_plan(target)
    tags = body.get("asset_tags") or {}
    # Backward-compatible: a single asset_tag when exactly one kind is needed.
    if not tags and body.get("asset_tag") and len(plan) == 1:
        tags = {plan[0]["kind"]: body["asset_tag"]}

    chosen, used_tags = [], set()
    for item in plan:
        kind = item["kind"]
        tag = (tags.get(kind) or "").strip()
        if not tag:
            return None, (jsonify(error=f"A {kind} asset tag is required."), 400)
        cand = db.query("SELECT * FROM assets WHERE asset_tag=%s", (tag,), fetch="one")
        if not cand:
            return None, (jsonify(error=f"No such asset '{tag}'."), 404)
        if cand["kind"] != kind:
            return None, (jsonify(error=f"Asset '{tag}' is a {cand['kind']}, but a {kind} is required here."), 409)
        if cand["status"] != "available":
            return None, (jsonify(error=f"Asset '{tag}' is not available (status: {cand['status']})."), 409)
        if tag in used_tags:
            return None, (jsonify(error=f"Asset '{tag}' was selected more than once."), 409)
        used_tags.add(tag)
        chosen.append(cand)
    return chosen, None


def provision_plan(target):
    """Kinds the employee needs a new asset for, and available tags per kind."""
    need = employee_asset_kinds_needed(target)
    held_kinds = {a["kind"] for a in held_assets(target)}
    missing = sorted(need - held_kinds)
    plan = []
    for kind in missing:
        avail = db.query("SELECT asset_tag FROM assets WHERE kind=%s AND status='available' ORDER BY asset_tag", (kind,))
        plan.append({"kind": kind, "available": [a["asset_tag"] for a in avail]})
    return plan


@bp.post("/api/requests/<int:rid>/provision")
@require_auth
def provision_asset(rid):
    """EUSS provisions assets. A VALID tag is REQUIRED for each resource kind the
    employee still needs, and each tag is checked to be an available asset of
    exactly that kind. Any no-longer-needed asset is released."""
    req, err = load_stage(rid, "pending_euss")
    if err:
        return err
    if req["type"] not in ("grant", "onboard", "shift"):
        return jsonify(error="This request is fulfilled by retrieval, not provisioning"), 409
    target = req["target_employee_id"]
    chosen, err = _validate_tags(target, request.get_json(silent=True) or {})
    if err:
        return err

    primary = None
    for asset in chosen:
        assign_asset(asset, target, rid)
        primary = primary or asset
    release_unneeded_assets(target, rid)  # e.g. a shift that dropped a kind
    if primary:
        db.query("UPDATE requests SET asset_id=%s WHERE id=%s", (primary["id"], rid), fetch=None)
    set_status(rid, "completed")
    tags_txt = ", ".join(a["asset_tag"] for a in chosen) if chosen else "no new assets (already held)"
    add_event(rid, g.user["id"], "provisioned", f"Provisioned {tags_txt}")
    return jsonify(request_dict(get_joined(rid)))


@bp.post("/api/requests/<int:rid>/retrieve")
@require_auth
def retrieve_asset(rid):
    """EUSS retrieves the asset for a return: revoke the service access, then
    release any now-unneeded asset. Ends the request."""
    req, err = load_stage(rid, "pending_euss")
    if err:
        return err
    if req["type"] != "return":
        return jsonify(error="This request is fulfilled by provisioning, not retrieval"), 409
    target = req["target_employee_id"]
    before = {a["id"]: a["asset_tag"] for a in held_assets(target)}
    # Privilege hierarchy: returning the laptop (higher) leaves the account intact;
    # returning the account (lower) takes the laptop with it.
    removed = revoke_with_dependents(target, req["service_id"])
    release_unneeded_assets(target, rid)
    after = {a["id"] for a in held_assets(target)}
    released = [tag for aid, tag in before.items() if aid not in after]
    add_event(rid, g.user["id"], "retrieved",
              f"Revoked {service_names(removed)}; retrieved {', '.join(released) if released else 'no asset'}")
    set_status(rid, "completed")
    return jsonify(request_dict(get_joined(rid)))
