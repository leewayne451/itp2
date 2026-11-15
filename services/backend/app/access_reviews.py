"""Access review and reconciliation domain logic (CHANGE 010).

The first iteration deliberately keeps campaigns small: one department, one
assigned Director, and the two resource kinds already modelled by the POC. A
Director's removal decision starts the existing governed return workflow; that
workflow, not the review itself, revokes access and records asset retrieval.
"""
import json
from datetime import datetime, timezone

from . import audit, db, workflows
from .core import (add_asset_event, department_effective_services, held_assets,
                   access_review_system_actor)


RISK_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3}
FINDING_LABELS = {
    "inactive_access": "Inactive employee still holds access",
    "asset_without_access": "Assigned asset has no matching entitlement",
    "orphan_entitlement": "Entitlement has no completed approval request",
    "outside_standard": "Access is outside the department standard",
    "missing_expected": "Expected department access is missing",
    "aligned": "Access matches the department standard",
}

RETURN_REMEDIATION = {
    "pending_hr": "return_pending_hr",
    "pending_director": "return_pending_director",
    "pending_aa": "return_pending_aa",
    "pending_it": "return_pending_it",
    "pending_appointment": "return_pending_appointment",
    "pending_euss": "return_pending_euss",
}


class ReviewError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.message = message
        self.status = status


def classify(status, actual, expected, has_asset, source_request_id):
    """Return every applicable finding plus the highest-risk primary finding."""
    findings = []
    if status != "active" and actual:
        findings.append("inactive_access")
    if has_asset and not actual:
        findings.append("asset_without_access")
    if actual and not source_request_id:
        findings.append("orphan_entitlement")
    if actual and not expected:
        findings.append("outside_standard")
    if expected and not actual:
        findings.append("missing_expected")
    if not findings:
        findings.append("aligned")
    severity = {
        "inactive_access": "critical", "asset_without_access": "high",
        "orphan_entitlement": "high", "outside_standard": "medium",
        "missing_expected": "medium", "aligned": "low",
    }
    primary = min(findings, key=lambda code: RISK_ORDER[severity[code]])
    return findings, primary, severity[primary]


def _source_request(employee_id, service, department_id):
    """Best available provenance for an entitlement created before explicit
    grant provenance existed. Laptop grants also support their coupled Account.
    """
    row = db.query(
        """SELECT r.id
             FROM requests r
             LEFT JOIN services requested ON requested.id=r.service_id
            WHERE r.target_employee_id=%s AND r.status='completed'
              AND (
                    (r.type='grant' AND r.service_id=%s)
                 OR (r.type='grant' AND %s='account' AND requested.resource_type='laptop')
                 OR (r.type IN ('onboard','shift') AND r.target_department_id=%s)
              )
            ORDER BY r.updated_at DESC, r.id DESC LIMIT 1""",
        (employee_id, service["id"], service["resource_type"], department_id), fetch="one")
    return row["id"] if row else None


def _snapshot_rows(department_id):
    services = db.query("SELECT id, name, resource_type FROM services WHERE resource_type IN ('laptop','account')")
    employees = db.query(
        """SELECT id, username, full_name, status
             FROM employees WHERE department_id=%s AND role <> 'system'
            ORDER BY full_name""", (department_id,))
    expected_ids = set(department_effective_services(department_id))
    rows = []
    for emp in employees:
        actual_ids = {r["service_id"] for r in db.query(
            "SELECT service_id FROM employee_access WHERE employee_id=%s", (emp["id"],))}
        assets = held_assets(emp["id"])
        asset_by_kind = {a["kind"].lower(): a for a in assets}
        for service in services:
            actual = service["id"] in actual_ids
            expected = service["id"] in expected_ids
            asset = asset_by_kind.get(service["resource_type"])
            if not (actual or expected or asset):
                continue
            source = _source_request(emp["id"], service, department_id) if actual else None
            findings, primary, risk = classify(emp["status"], actual, expected, bool(asset), source)
            rows.append({
                "employee_id": emp["id"], "employee_username": emp["username"],
                "employee_name": emp["full_name"], "employee_status": emp["status"],
                "service_id": service["id"], "service_name": service["name"],
                "actual_access": actual, "expected_access": expected,
                "source_request_id": source, "asset_id": asset["id"] if asset else None,
                "asset_tag": asset["asset_tag"] if asset else None,
                "findings": findings, "finding_code": primary, "risk_level": risk,
            })
    return rows


def _same_snapshot(item, live):
    return all(item[k] == live[k] for k in
               ("actual_access", "expected_access", "source_request_id", "asset_id", "finding_code"))


def create_campaign(name, department_id, reviewer_id, due_at, created_by):
    name = (name or "").strip()
    if not name:
        raise ReviewError("Campaign name is required")
    dept = db.query("SELECT id, name FROM departments WHERE id=%s", (department_id,), fetch="one")
    if not dept:
        raise ReviewError("Department not found", 404)
    existing = db.query(
        """SELECT id, name, status FROM access_review_campaigns
            WHERE department_id=%s AND status IN ('in_review','remediation','overdue')
            ORDER BY created_at DESC LIMIT 1""", (department_id,), fetch="one")
    if existing:
        raise ReviewError(
            f"{dept['name']} already has an open access review: "
            f"#{existing['id']} {existing['name']} ({existing['status'].replace('_', ' ')}). "
            "Complete that campaign before creating another one.", 409)
    reviewer = db.query("SELECT id, username, full_name, role, status FROM employees WHERE id=%s",
                        (reviewer_id,), fetch="one")
    if not reviewer or reviewer["role"] != "director" or reviewer["status"] != "active":
        raise ReviewError("Reviewer must be an active Director")
    try:
        parsed_due = datetime.fromisoformat(str(due_at).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        raise ReviewError("A valid due date and time is required")
    if not parsed_due.tzinfo:
        parsed_due = parsed_due.replace(tzinfo=timezone.utc)
    if parsed_due <= datetime.now(timezone.utc):
        raise ReviewError("Due date must be in the future")
    try:
        row = db.query(
            """INSERT INTO access_review_campaigns
                 (name, department_id, reviewer_id, created_by, due_at)
               VALUES (%s,%s,%s,%s,%s) RETURNING id""",
            (name, department_id, reviewer_id, created_by, parsed_due), fetch="one")
    except Exception as exc:
        # The partial unique index also closes the small race between the check
        # above and insertion when two administrators submit simultaneously.
        if getattr(exc, "pgcode", None) == "23505":
            raise ReviewError(
                f"{dept['name']} already has an open access review. "
                "Complete it before creating another one.", 409) from exc
        raise
    count = reconcile(row["id"], reset_changed=False)
    audit.record("access", "access_review_created", target_type="access_review", target_id=row["id"],
                 target_label=name, details={"department": dept["name"], "reviewer": reviewer["username"],
                                             "due_at": parsed_due.isoformat(), "items": count})
    return row["id"]


def _sync_linked_returns(campaign_id):
    """Reflect workflow progress in review evidence without changing access itself.

    The return workflow remains the sole authority that revokes access and releases
    assets. A rejected return reopens the certification item for the Director.
    """
    rows = db.query(
        """SELECT i.id, i.decision, i.decision_reason, i.remediation_status,
                  i.return_request_id, r.status AS return_status
             FROM access_review_items i
             JOIN requests r ON r.id=i.return_request_id
            WHERE i.campaign_id=%s""", (campaign_id,))
    for row in rows:
        status = row["return_status"]
        if status == "completed":
            if row["remediation_status"] != "remediated":
                db.query("""UPDATE access_review_items SET remediation_status='remediated',
                              remediated_at=now() WHERE id=%s""", (row["id"],), fetch=None)
        elif status == "rejected":
            # Preserve the original decision in the request and audit log, but let the
            # reviewer choose a new certification outcome for the still-live access.
            if row["decision"] == "revoke":
                note = (row["decision_reason"] or "") + f"\nLinked return #{row['return_request_id']} was rejected."
                db.query("""UPDATE access_review_items SET decision=NULL, decision_reason=%s,
                              reviewed_by=NULL, reviewed_at=NULL, remediation_status='pending',
                              remediated_at=NULL WHERE id=%s""", (note, row["id"]), fetch=None)
                audit.record("access", "access_review_return_rejected", outcome="denied",
                             target_type="access_review_item", target_id=row["id"],
                             details={"campaign_id": campaign_id, "return_request_id": row["return_request_id"]})
        else:
            remediation = RETURN_REMEDIATION.get(status)
            if remediation and row["remediation_status"] != remediation:
                db.query("UPDATE access_review_items SET remediation_status=%s WHERE id=%s",
                         (remediation, row["id"]), fetch=None)


def reconcile(campaign_id, reset_changed=True):
    campaign = db.query("SELECT * FROM access_review_campaigns WHERE id=%s", (campaign_id,), fetch="one")
    if not campaign:
        raise ReviewError("Access review not found", 404)
    if campaign["status"] == "completed":
        raise ReviewError("Completed reviews cannot be reconciled", 409)
    _sync_linked_returns(campaign_id)
    live_rows = _snapshot_rows(campaign["department_id"])
    changed = new = 0
    for live in live_rows:
        old = db.query(
            "SELECT * FROM access_review_items WHERE campaign_id=%s AND employee_id=%s AND service_id=%s",
            (campaign_id, live["employee_id"], live["service_id"]), fetch="one")
        values = (live["actual_access"], live["expected_access"], live["source_request_id"],
                  live["asset_id"], live["finding_code"], json.dumps(live["findings"]), live["risk_level"])
        if not old:
            db.query(
                """INSERT INTO access_review_items
                     (campaign_id, employee_id, service_id, actual_access, expected_access,
                      source_request_id, asset_id, finding_code, findings, risk_level)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s)""",
                (campaign_id, live["employee_id"], live["service_id"]) + values, fetch=None)
            new += 1
        elif not _same_snapshot(old, live) and reset_changed:
            # A successful revoke (and the later physical collection) is itself
            # expected to change the live snapshot. Preserve that decision while
            # refreshing the evidence. Any other drift invalidates the old answer
            # and must be reviewed again.
            preserve_revoke = old["decision"] == "revoke" and not live["actual_access"]
            if preserve_revoke:
                db.query(
                    """UPDATE access_review_items SET
                         actual_access=%s, expected_access=%s, source_request_id=%s, asset_id=%s,
                         finding_code=%s, findings=%s::jsonb, risk_level=%s,
                         last_reconciled_at=now()
                       WHERE id=%s""", values + (old["id"],), fetch=None)
            else:
                db.query(
                    """UPDATE access_review_items SET
                         actual_access=%s, expected_access=%s, source_request_id=%s, asset_id=%s,
                         finding_code=%s, findings=%s::jsonb, risk_level=%s,
                         decision=NULL, decision_reason=NULL, reviewed_by=NULL, reviewed_at=NULL,
                         remediation_status='pending', remediated_at=NULL, last_reconciled_at=now()
                       WHERE id=%s""", values + (old["id"],), fetch=None)
            changed += 1
    db.query("UPDATE access_review_campaigns SET last_reconciled_at=now() WHERE id=%s",
             (campaign_id,), fetch=None)
    if reset_changed:
        audit.record("access", "access_review_reconciled", target_type="access_review",
                     target_id=campaign_id, target_label=campaign["name"],
                     details={"items_scanned": len(live_rows), "new_items": new, "changed_items": changed})
    _sync_linked_returns(campaign_id)
    return len(live_rows)


_DETAIL_SQL = """
 SELECT c.*, d.name AS department_name,
        rv.full_name AS reviewer_name, rv.username AS reviewer_username,
        cr.full_name AS created_by_name
   FROM access_review_campaigns c
   JOIN departments d ON d.id=c.department_id
   JOIN employees rv ON rv.id=c.reviewer_id
   JOIN employees cr ON cr.id=c.created_by
"""


def campaigns_for(user):
    if user["role"] == "director":
        rows = db.query(_DETAIL_SQL + " WHERE c.reviewer_id=%s ORDER BY c.created_at DESC", (user["id"],))
    elif user["role"] == "it_admin":
        rows = db.query(_DETAIL_SQL + " ORDER BY c.created_at DESC")
    else:
        return []
    return [_campaign_dict(r, include_items=False) for r in rows]


def get_campaign(campaign_id, user):
    row = db.query(_DETAIL_SQL + " WHERE c.id=%s", (campaign_id,), fetch="one")
    if not row:
        raise ReviewError("Access review not found", 404)
    if user["role"] == "director" and row["reviewer_id"] != user["id"]:
        raise ReviewError("This review is assigned to another Director", 403)
    if user["role"] not in ("director", "it_admin"):
        raise ReviewError("Access review access denied", 403)
    _sync_linked_returns(campaign_id)
    row = db.query(_DETAIL_SQL + " WHERE c.id=%s", (campaign_id,), fetch="one")
    return _campaign_dict(row, include_items=True)


def _campaign_dict(row, include_items=False):
    status = row["status"]
    if status not in ("completed", "overdue") and row["due_at"] < datetime.now(timezone.utc):
        status = "overdue"
    out = {
        "id": row["id"], "name": row["name"], "status": status,
        "department_id": row["department_id"], "department_name": row["department_name"],
        "reviewer_id": row["reviewer_id"], "reviewer_name": row["reviewer_name"],
        "reviewer_username": row["reviewer_username"], "created_by_name": row["created_by_name"],
        "due_at": row["due_at"].isoformat(), "snapshot_at": row["snapshot_at"].isoformat(),
        "last_reconciled_at": row["last_reconciled_at"].isoformat() if row["last_reconciled_at"] else None,
        "completed_at": row["completed_at"].isoformat() if row["completed_at"] else None,
        "created_at": row["created_at"].isoformat(),
    }
    if include_items:
        items = db.query(
            """SELECT i.*, e.full_name AS employee_name, e.username AS employee_username,
                      e.status AS employee_status, s.name AS service_name, a.asset_tag,
                      rb.full_name AS reviewed_by_name, rr.status AS return_status
                 FROM access_review_items i
                 JOIN employees e ON e.id=i.employee_id
                 JOIN services s ON s.id=i.service_id
                 LEFT JOIN assets a ON a.id=i.asset_id
                 LEFT JOIN employees rb ON rb.id=i.reviewed_by
                 LEFT JOIN requests rr ON rr.id=i.return_request_id
                WHERE i.campaign_id=%s
                ORDER BY CASE i.risk_level WHEN 'critical' THEN 1 WHEN 'high' THEN 2
                         WHEN 'medium' THEN 3 ELSE 4 END, e.full_name, s.name""", (row["id"],))
        out["items"] = [_item_dict(i) for i in items]
        out["summary"] = {
            "total": len(items), "pending": sum(i["decision"] is None for i in items),
            "kept": sum(i["decision"] == "keep" for i in items),
            "revoked": sum(i["decision"] == "revoke" for i in items),
            # Count only work that is still open. An Investigate decision remains
            # part of the evidence after IT resolves it, but is no longer a blocker.
            "investigating": sum(i["remediation_status"] == "investigating" for i in items),
            "pending_collection": sum(i["remediation_status"] == "pending_collection" for i in items),
            "unresolved": sum(i["remediation_status"] not in ("accepted", "remediated") for i in items),
        }
    return out


def _item_dict(row):
    findings = row["findings"] if isinstance(row["findings"], list) else json.loads(row["findings"])
    return {
        "id": row["id"], "employee_id": row["employee_id"],
        "employee_name": row["employee_name"], "employee_username": row["employee_username"],
        "employee_status": row["employee_status"], "service_id": row["service_id"],
        "service_name": row["service_name"], "actual_access": row["actual_access"],
        "expected_access": row["expected_access"], "source_request_id": row["source_request_id"],
        "asset_id": row["asset_id"], "asset_tag": row["asset_tag"],
        "finding_code": row["finding_code"], "finding_label": FINDING_LABELS[row["finding_code"]],
        "findings": findings, "finding_labels": [FINDING_LABELS[f] for f in findings],
        "risk_level": row["risk_level"], "decision": row["decision"],
        "decision_reason": row["decision_reason"], "reviewed_by_name": row["reviewed_by_name"],
        "reviewed_at": row["reviewed_at"].isoformat() if row["reviewed_at"] else None,
        "remediation_status": row["remediation_status"],
        "return_request_id": row.get("return_request_id"),
        "return_status": row.get("return_status"),
    }


def decide(item_id, reviewer, decision, reason):
    if decision not in ("keep", "revoke", "investigate"):
        raise ReviewError("Decision must be keep, revoke or investigate")
    reason = (reason or "").strip()
    item = db.query(
        """SELECT i.*, c.name AS campaign_name, c.status AS campaign_status,
                  c.department_id, c.reviewer_id, e.username, e.full_name,
                  s.name AS service_name, s.resource_type
             FROM access_review_items i
             JOIN access_review_campaigns c ON c.id=i.campaign_id
             JOIN employees e ON e.id=i.employee_id
             JOIN services s ON s.id=i.service_id WHERE i.id=%s""", (item_id,), fetch="one")
    if not item:
        raise ReviewError("Review item not found", 404)
    if item["campaign_status"] == "completed":
        raise ReviewError("This review is already completed", 409)
    if item["reviewer_id"] != reviewer["id"]:
        raise ReviewError("This item is assigned to another Director", 403)
    if item["employee_id"] == reviewer["id"]:
        audit.record("access", "access_review_self_review_blocked", outcome="denied",
                     target_type="access_review_item", target_id=item_id,
                     target_label=f"{item['full_name']} / {item['service_name']}")
        raise ReviewError("You cannot review your own access", 403)
    if decision in ("revoke", "investigate") and not reason:
        raise ReviewError("A reason is required for this decision")
    if decision == "keep" and item["finding_code"] != "aligned" and not reason:
        raise ReviewError("A reason is required to keep flagged access")

    live = next((r for r in _snapshot_rows(item["department_id"])
                 if r["employee_id"] == item["employee_id"] and r["service_id"] == item["service_id"]), None)
    if live is None:
        live = {"actual_access": False, "expected_access": False, "source_request_id": None,
                "asset_id": None, "finding_code": "aligned"}
    if not _same_snapshot(item, live):
        reconcile(item["campaign_id"], reset_changed=True)
        audit.record("access", "access_review_changed_since_snapshot", outcome="denied",
                     target_type="access_review_item", target_id=item_id,
                     target_label=f"{item['full_name']} / {item['service_name']}")
        raise ReviewError("This access changed since the review snapshot. The item was refreshed; review it again.", 409)
    if decision == "keep" and not item["actual_access"]:
        raise ReviewError("Keep applies only to access the employee currently holds; investigate missing access instead")
    if decision == "revoke" and not item["actual_access"]:
        raise ReviewError("There is no current access to revoke", 409)

    remediation = "accepted" if decision == "keep" else "investigating"
    return_request_id = item.get("return_request_id")
    if decision == "revoke":
        linked = (db.query("SELECT id, status FROM requests WHERE id=%s", (return_request_id,), fetch="one")
                  if return_request_id else None)
        if linked and linked["status"] not in ("completed", "rejected"):
            remediation = RETURN_REMEDIATION.get(linked["status"], "return_pending_hr")
        else:
            system = access_review_system_actor()
            if not system:
                raise ReviewError("Access-review system account is missing (restart the backend to seed it)", 503)
            justification = (f"Access review #{item['campaign_id']} item #{item_id}: Director requested return of "
                             f"{item['service_name']}. Reason: {reason}")
            return_request_id, error = workflows.raise_return(
                system["id"], {"id": item["employee_id"]}, item["service_id"], justification, on_behalf=True)
            if error:
                raise ReviewError(error, 409)
            remediation = "return_pending_hr"
    db.query(
        """UPDATE access_review_items SET decision=%s, decision_reason=%s, reviewed_by=%s,
             reviewed_at=now(), remediation_status=%s,
             return_request_id=%s,
             remediated_at=CASE WHEN %s IN ('accepted','remediated') THEN now() ELSE NULL END
           WHERE id=%s""", (decision, reason or None, reviewer["id"], remediation,
                              return_request_id, remediation, item_id), fetch=None)
    if decision == "revoke":
        db.query("UPDATE access_review_campaigns SET status='remediation' WHERE id=%s",
                 (item["campaign_id"],), fetch=None)
    audit.record("access", f"access_review_item_{decision}", target_type="access_review_item",
                 target_id=item_id, target_label=f"{item['full_name']} / {item['service_name']}",
                 details={"campaign_id": item["campaign_id"], "reason": reason,
                          "finding": item["finding_code"],
                          "return_request_id": return_request_id})
    return {"decision": decision, "return_request_id": return_request_id}


def resolve_investigation(item_id, actor, note):
    """Let IT record the outcome of an investigated discrepancy.

    This does not silently grant access. If the investigation requires a grant,
    it still goes through the normal request and approval workflow; reconciliation
    will detect that live change and return the item to the Director.
    """
    note = (note or "").strip()
    if not note:
        raise ReviewError("A resolution note is required")
    item = db.query(
        """SELECT i.*, c.name AS campaign_name, c.status AS campaign_status,
                  e.full_name, s.name AS service_name
             FROM access_review_items i
             JOIN access_review_campaigns c ON c.id=i.campaign_id
             JOIN employees e ON e.id=i.employee_id
             JOIN services s ON s.id=i.service_id WHERE i.id=%s""", (item_id,), fetch="one")
    if not item:
        raise ReviewError("Review item not found", 404)
    if item["campaign_status"] == "completed":
        raise ReviewError("This review is already completed", 409)
    if item["decision"] != "investigate" or item["remediation_status"] != "investigating":
        raise ReviewError("Only an open investigation can be resolved", 409)
    combined = f"{item['decision_reason']}\nResolution: {note}"
    db.query(
        """UPDATE access_review_items SET decision_reason=%s,
             remediation_status='remediated', remediated_at=now() WHERE id=%s""",
        (combined, item_id), fetch=None)
    audit.record("access", "access_review_investigation_resolved",
                 target_type="access_review_item", target_id=item_id,
                 target_label=f"{item['full_name']} / {item['service_name']}",
                 details={"campaign_id": item["campaign_id"], "note": note,
                          "resolved_by": actor["username"]})


def asset_tasks(status=None):
    where, params = "", ()
    if status in ("pending", "retrieved"):
        where, params = " WHERE t.status=%s", (status,)
    rows = db.query(
        """SELECT t.*, a.asset_tag, a.kind, e.full_name AS employee_name,
                  e.username AS employee_username, d.name AS department_name,
                  i.campaign_id, i.service_id, s.name AS service_name, c.name AS campaign_name
             FROM access_review_asset_tasks t
             JOIN assets a ON a.id=t.asset_id
             JOIN employees e ON e.id=t.employee_id
             LEFT JOIN departments d ON d.id=e.department_id
             JOIN access_review_items i ON i.id=t.review_item_id
             JOIN services s ON s.id=i.service_id
             JOIN access_review_campaigns c ON c.id=i.campaign_id""" + where +
        " ORDER BY CASE t.status WHEN 'pending' THEN 1 ELSE 2 END, t.created_at", params)
    return [{**r, "created_at": r["created_at"].isoformat(),
             "completed_at": r["completed_at"].isoformat() if r["completed_at"] else None} for r in rows]


def collect_asset(task_id, actor):
    task = db.query(
        """SELECT t.*, a.asset_tag, a.status AS asset_status, a.assigned_to,
                  i.campaign_id, c.name AS campaign_name
             FROM access_review_asset_tasks t
             JOIN assets a ON a.id=t.asset_id
             JOIN access_review_items i ON i.id=t.review_item_id
             JOIN access_review_campaigns c ON c.id=i.campaign_id WHERE t.id=%s""",
        (task_id,), fetch="one")
    if not task:
        raise ReviewError("Collection task not found", 404)
    if task["status"] != "pending":
        raise ReviewError("This asset has already been collected", 409)
    if task["assigned_to"] != task["employee_id"] or task["asset_status"] != "assigned":
        raise ReviewError("Asset custody changed; reconcile the review before collecting", 409)
    db.query("UPDATE assets SET status='available', assigned_to=NULL WHERE id=%s", (task["asset_id"],), fetch=None)
    add_asset_event(task["asset_id"], task["employee_id"], "review_retrieved", None)
    db.query("UPDATE access_review_asset_tasks SET status='retrieved', completed_by=%s, completed_at=now() WHERE id=%s",
             (actor["id"], task_id), fetch=None)
    pending = db.query("SELECT 1 FROM access_review_asset_tasks WHERE review_item_id=%s AND status='pending'",
                       (task["review_item_id"],), fetch="one")
    if not pending:
        db.query("UPDATE access_review_items SET remediation_status='remediated', remediated_at=now() WHERE id=%s",
                 (task["review_item_id"],), fetch=None)
    audit.record("asset", "access_review_asset_retrieved", target_type="asset",
                 target_id=task["asset_id"], target_label=task["asset_tag"],
                 details={"review_item_id": task["review_item_id"], "campaign_id": task["campaign_id"],
                          "employee_id": task["employee_id"]})


def close_campaign(campaign_id):
    campaign = db.query("SELECT * FROM access_review_campaigns WHERE id=%s", (campaign_id,), fetch="one")
    if not campaign:
        raise ReviewError("Access review not found", 404)
    if campaign["status"] == "completed":
        return
    reconcile(campaign_id, reset_changed=True)
    blockers = db.query(
        """SELECT
             COUNT(*) FILTER (WHERE decision IS NULL) AS undecided,
             COUNT(*) FILTER (WHERE remediation_status NOT IN ('accepted','remediated')) AS unresolved
           FROM access_review_items WHERE campaign_id=%s""", (campaign_id,), fetch="one")
    if blockers["undecided"] or blockers["unresolved"]:
        raise ReviewError(
            f"Review cannot close: {blockers['undecided']} undecided and {blockers['unresolved']} unresolved item(s)", 409)
    db.query("UPDATE access_review_campaigns SET status='completed', completed_at=now() WHERE id=%s",
             (campaign_id,), fetch=None)
    audit.record("access", "access_review_completed", target_type="access_review",
                 target_id=campaign_id, target_label=campaign["name"])

