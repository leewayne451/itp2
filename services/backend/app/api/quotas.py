"""Quota figures (AA + IT Admin) and quota-change requests (AA proposes, IT decides)."""
from flask import Blueprint, request, jsonify, g

from .. import audit, db
from ..auth import require_role
from ..core import (quota_used, quota_allowed, QR_SELECT, quota_request_dict,
                    dept_quota_allowed, dept_quota_used, set_department_quota)

bp = Blueprint("quotas_api", __name__)


@bp.get("/api/department-quotas")
@require_role("it_admin", "admin", "aa")
def list_department_quotas():
    """Per-department capacity for each resource, with live usage."""
    services = db.query("SELECT id, name, resource_type FROM services "
                        "WHERE resource_type IN ('laptop','account') ORDER BY id")
    out = []
    for d in db.query("SELECT id, name FROM departments ORDER BY name"):
        rows = []
        for s in services:
            allowed = dept_quota_allowed(d["id"], s["id"])
            used = dept_quota_used(d["id"], s["id"])
            rows.append({"service_id": s["id"], "name": s["name"], "resource_type": s["resource_type"],
                         "allowed": allowed, "used": used, "remaining": allowed - used})
        out.append({"department_id": d["id"], "department_name": d["name"], "resources": rows})
    return jsonify(out)


@bp.put("/api/departments/<int:dept_id>/quotas")
@require_role("it_admin", "admin")
def update_department_quotas(dept_id):
    """IT Admin sets the laptop/account capacity for one department."""
    dept = db.query("SELECT name FROM departments WHERE id=%s", (dept_id,), fetch="one")
    if not dept:
        return jsonify(error="Department not found"), 404
    quotas = (request.get_json(silent=True) or {}).get("quotas") or {}
    if not quotas:
        return jsonify(error="No quotas supplied"), 400
    for raw_sid, raw_allowed in quotas.items():
        try:
            sid, allowed = int(raw_sid), int(raw_allowed)
        except (TypeError, ValueError):
            return jsonify(error="Quota values must be whole numbers"), 400
        if allowed < 0:
            return jsonify(error="A quota cannot be negative"), 400
        svc = db.query("SELECT name, resource_type FROM services WHERE id=%s", (sid,), fetch="one")
        if not svc:
            return jsonify(error="Unknown resource"), 404
        if svc["resource_type"] not in ("laptop", "account"):
            return jsonify(error="Only laptop and account have department quotas"), 400
        old = dept_quota_allowed(dept_id, sid)
        set_department_quota(dept_id, sid, allowed)
        if old != allowed:
            audit.record("quota", "department_quota_changed", target_type="department", target_id=dept_id,
                         target_label=dept["name"],
                         details={"resource": svc["name"], "from": old, "to": allowed,
                                  "in_use": dept_quota_used(dept_id, sid)})
    return jsonify(ok=True, department_id=dept_id)


@bp.get("/api/quotas")
@require_role("aa", "it_admin", "admin")
def list_quotas():
    svcs = db.query("SELECT id, key, name, resource_type FROM services ORDER BY id")
    out = []
    for s in svcs:
        allowed, used = quota_allowed(s["id"]), quota_used(s["id"])
        out.append({"service_id": s["id"], "key": s["key"], "name": s["name"], "resource_type": s["resource_type"],
                    "allowed": allowed, "used": used, "remaining": allowed - used})
    return jsonify(out)


@bp.post("/api/quota-requests")
@require_role("aa")
def create_quota_request():
    body = request.get_json(silent=True) or {}
    service_id = body.get("service_id")
    new_allowed = body.get("new_allowed")
    reason = (body.get("reason") or "").strip()
    if service_id is None or new_allowed is None:
        return jsonify(error="service_id and new_allowed are required"), 400
    try:
        new_allowed = int(new_allowed)
    except (TypeError, ValueError):
        return jsonify(error="new_allowed must be a number"), 400
    if new_allowed < 0:
        return jsonify(error="new_allowed cannot be negative"), 400
    q = db.query("SELECT allowed FROM quotas WHERE service_id=%s", (service_id,), fetch="one")
    if not q:
        return jsonify(error="Unknown resource"), 404
    if new_allowed == q["allowed"]:
        return jsonify(error="That is already the current quota"), 400
    if db.query("SELECT 1 FROM quota_requests WHERE service_id=%s AND status='pending'", (service_id,), fetch="one"):
        return jsonify(error="A quota change for this resource is already pending"), 409
    row = db.query(
        """INSERT INTO quota_requests (service_id, requested_by, current_allowed, new_allowed, reason)
           VALUES (%s,%s,%s,%s,%s) RETURNING id""",
        (service_id, g.user["id"], q["allowed"], new_allowed, reason), fetch="one")
    svc = db.query("SELECT name FROM services WHERE id=%s", (service_id,), fetch="one")
    audit.record("quota", "org_quota_change_proposed", target_type="quota_request", target_id=row["id"],
                 target_label=f"{svc['name']} org-wide",
                 details={"resource": svc["name"], "from": q["allowed"], "to": new_allowed, "reason": reason})
    return jsonify(id=row["id"]), 201


@bp.get("/api/quota-requests")
@require_role("aa", "it_admin", "admin")
def list_quota_requests():
    return jsonify([quota_request_dict(r) for r in db.query(QR_SELECT + " ORDER BY qr.created_at DESC")])


def _decide(qid, approve):
    qr = db.query("SELECT * FROM quota_requests WHERE id=%s", (qid,), fetch="one")
    if not qr:
        return jsonify(error="Not found"), 404
    if qr["status"] != "pending":
        return jsonify(error="Already decided"), 409
    note = ((request.get_json(silent=True) or {}).get("note") or "").strip()
    if approve:
        db.query("UPDATE quotas SET allowed=%s WHERE service_id=%s", (qr["new_allowed"], qr["service_id"]), fetch=None)
    db.query("UPDATE quota_requests SET status=%s, decided_by=%s, decide_note=%s, updated_at=now() WHERE id=%s",
             ("approved" if approve else "rejected", g.user["id"], note or (None if approve else "No reason given"), qid),
             fetch=None)
    svc = db.query("SELECT name FROM services WHERE id=%s", (qr["service_id"],), fetch="one")
    proposer = db.query("SELECT username FROM employees WHERE id=%s", (qr["requested_by"],), fetch="one")
    audit.record("quota", "org_quota_change_approved" if approve else "org_quota_change_rejected",
                 outcome="success" if approve else "denied", target_type="quota_request", target_id=qid,
                 target_label=f"{svc['name']} org-wide",
                 details={"resource": svc["name"], "from": qr["current_allowed"], "to": qr["new_allowed"],
                          "applied": approve, "note": note or None,
                          "proposed_by": proposer and proposer["username"]})
    return jsonify(ok=True)


@bp.post("/api/quota-requests/<int:qid>/approve")
@require_role("it_admin")
def approve_quota_request(qid):
    return _decide(qid, True)


@bp.post("/api/quota-requests/<int:qid>/reject")
@require_role("it_admin")
def reject_quota_request(qid):
    return _decide(qid, False)
