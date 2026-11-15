"""Automatic enforcement for time-bound access grants.

The database remains the source of truth.  The worker removes an entitlement only
when the exact expiry timestamp it observed is still due, so a concurrent extension
or manual return cannot be overwritten. Physical custody changes only after EUSS
confirms a collection task; a re-grant before collection cancels that task.
"""
import threading
import time

from . import audit, db
from .config import ACCESS_EXPIRY_INTERVAL_SECONDS
from .core import (add_asset_event, employee_asset_kinds_needed, held_assets,
                   revoke_access, service_by_type)

SYSTEM = {"id": None, "username": "access-expiry-worker", "role": "system"}


def _asset_label(asset):
    return f"{asset['kind']} {asset['asset_tag']}"


def create_collection_tasks(emp_id, trigger_service_id=None, actor=None,
                            reason="Digital access expired; EUSS must confirm physical collection"):
    """Queue each held asset that is no longer backed by digital access."""
    needed = employee_asset_kinds_needed(emp_id)
    created = 0
    for asset in held_assets(emp_id):
        if asset["kind"] in needed:
            continue
        row = db.query(
            """INSERT INTO asset_collection_tasks
                   (asset_id,employee_id,trigger_service_id,reason)
               VALUES (%s,%s,%s,%s)
               ON CONFLICT (asset_id) WHERE status='pending' DO NOTHING
               RETURNING id""",
            (asset["id"], emp_id, trigger_service_id, reason), fetch="one")
        if not row:
            continue
        created += 1
        audit.record("asset", "asset_collection_requested", actor=actor or SYSTEM,
                     target_type="asset", target_id=asset["asset_tag"],
                     target_label=_asset_label(asset),
                     details={"employee_id": emp_id, "collection_task_id": row["id"],
                              "trigger_service_id": trigger_service_id, "reason": reason})
    return created


def cancel_resolved_collections(emp_id, actor=None):
    """Cancel pending collections for asset kinds made necessary by a re-grant."""
    needed = employee_asset_kinds_needed(emp_id)
    if not needed:
        return 0
    rows = db.query(
        """UPDATE asset_collection_tasks t
              SET status='cancelled', completed_at=now(),
                  cancellation_reason='Access re-granted before physical collection'
             FROM assets a
            WHERE t.asset_id=a.id AND t.employee_id=%s AND t.status='pending'
              AND a.status='assigned' AND a.assigned_to=t.employee_id
              AND a.kind=ANY(%s)
        RETURNING t.id,a.asset_tag,a.kind""", (emp_id, list(needed)))
    for row in rows:
        audit.record("asset", "asset_collection_cancelled", actor=actor,
                     target_type="asset", target_id=row["asset_tag"],
                     target_label=_asset_label(row),
                     details={"employee_id": emp_id, "collection_task_id": row["id"],
                              "reason": "access re-granted before collection"})
    return len(rows)


def collection_tasks(status="pending"):
    return db.query(
        """SELECT t.id,t.status,t.reason,t.created_at,t.completed_at,
                  a.id AS asset_id,a.asset_tag,a.kind,a.assigned_to,
                  e.full_name AS employee_name,e.username,d.name AS department_name
             FROM asset_collection_tasks t
             JOIN assets a ON a.id=t.asset_id
             JOIN employees e ON e.id=t.employee_id
        LEFT JOIN departments d ON d.id=e.department_id
            WHERE t.status=%s
            ORDER BY t.created_at,t.id""", (status,))


def collect_asset(task_id, actor):
    """Complete one EUSS collection, unless access now requires the asset again."""
    task = db.query(
        """SELECT t.*,a.asset_tag,a.kind,a.status AS asset_status,a.assigned_to
             FROM asset_collection_tasks t JOIN assets a ON a.id=t.asset_id
            WHERE t.id=%s""", (task_id,), fetch="one")
    if not task:
        return None, "Collection task not found"
    if task["status"] != "pending":
        return None, "Collection task is no longer pending"
    if task["kind"] in employee_asset_kinds_needed(task["employee_id"]):
        cancel_resolved_collections(task["employee_id"], actor=actor)
        return None, "Collection cancelled because this employee now needs the asset again"
    if task["asset_status"] != "assigned" or task["assigned_to"] != task["employee_id"]:
        db.query("""UPDATE asset_collection_tasks SET status='cancelled',completed_at=now(),
                         cancellation_reason='Asset was no longer assigned to this employee'
                     WHERE id=%s AND status='pending'""", (task_id,), fetch=None)
        return None, "Asset is no longer assigned to this employee"
    db.query("UPDATE assets SET status='available',assigned_to=NULL WHERE id=%s",
             (task["asset_id"],), fetch=None)
    db.query("""UPDATE asset_collection_tasks SET status='collected',completed_at=now(),
                     completed_by_id=%s WHERE id=%s AND status='pending'""",
             (actor.get("id"), task_id), fetch=None)
    add_asset_event(task["asset_id"], task["employee_id"], "expiry_retrieved", None)
    return task, None


def expire_due(limit=100):
    """Revoke due entitlements and return the number actually removed.

    Account is the lower-level resource in this POC.  If it expires, laptop
    access must also go because a laptop entitlement cannot exist without an
    account.  Expiring a laptop alone leaves an independently held account.
    """
    rows = db.query(
        """SELECT ea.employee_id,ea.service_id,ea.expires_at,s.resource_type
             FROM employee_access ea JOIN services s ON s.id=ea.service_id
            WHERE ea.expires_at IS NOT NULL AND ea.expires_at<=now()
            ORDER BY CASE s.resource_type WHEN 'account' THEN 0 ELSE 1 END,
                     ea.expires_at,ea.employee_id,ea.service_id
            LIMIT %s""", (limit,))
    removed = 0
    affected = {}
    for row in rows:
        details = {"expires_at": row["expires_at"].isoformat(),
                   "reason": "time-bound grant reached its expiry"}
        if not revoke_access(row["employee_id"], row["service_id"],
                             audit_action="entitlement_expired", audit_details=details,
                             actor=SYSTEM, due_at=row["expires_at"]):
            continue
        removed += 1
        affected[row["employee_id"]] = row["service_id"]

        if row["resource_type"] == "account":
            laptop = service_by_type("laptop")
            if laptop and revoke_access(
                    row["employee_id"], laptop["id"],
                    audit_action="entitlement_expired",
                    audit_details={**details, "reason": "dependent on expired account",
                                   "trigger_service_id": row["service_id"]},
                    actor=SYSTEM):
                removed += 1
    for emp_id, trigger_service_id in affected.items():
        create_collection_tasks(emp_id, trigger_service_id)
    return removed


def start_background():
    def loop():
        while True:
            try:
                count = expire_due()
                if count:
                    print(f"[access-expiry] revoked {count} entitlement(s)", flush=True)
            except Exception as exc:  # noqa: BLE001 — a later cycle must retry
                print(f"[access-expiry] background error: {type(exc).__name__}: {exc}", flush=True)
            time.sleep(ACCESS_EXPIRY_INTERVAL_SECONDS)

    print(f"[access-expiry] worker interval={ACCESS_EXPIRY_INTERVAL_SECONDS}s", flush=True)
    threading.Thread(target=loop, name="access-expiry", daemon=True).start()
