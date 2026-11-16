"""Read-only, trend-based resource demand forecasting for IT capacity planning."""
import json
from collections import defaultdict

from . import db

LOOKBACK_DAYS = 90
HORIZON_DAYS = 90
ASSET_KIND = {"laptop": "Laptop", "account": "Account"}


def event_deltas(events):
    """Return net projected headcount movement per department from normalised events."""
    out = defaultdict(int)
    for event in events:
        if event["type"] == "hire" and event.get("department_id"):
            out[event["department_id"]] += 1
        elif event["type"] == "transfer":
            if event.get("department_id"):
                out[event["department_id"]] += 1
            if event.get("source_department_id"):
                out[event["source_department_id"]] -= 1
    return dict(out)


def resource_projection(current_used, projected_change, allowed, available_assets=None):
    """Calculate an advisory capacity recommendation without mutating anything."""
    projected = max(0, current_used + projected_change)
    extra_needed = max(0, projected_change)
    return {"projected_demand": projected,
            "quota_gap": max(0, projected - allowed),
            "recommended_quota": max(allowed, projected),
            "procurement_needed": (max(0, extra_needed - available_assets)
                                   if available_assets is not None else None)}


def _real_events(departments):
    """Normalise processed HRMS events. Shift requests retain a transfer's source."""
    rows = db.query(
        """SELECT h.event_id, h.event_type, h.payload, r.source_department_id
             FROM hr_events h
             LEFT JOIN requests r ON r.justification LIKE ('%%' || h.event_id || '%%')
            WHERE h.status='processed' AND h.event_type IN ('hire','transfer')
              AND COALESCE(h.processed_at, h.received_at) >= now() - (%s * interval '1 day')""",
        (LOOKBACK_DAYS,))
    out = []
    for row in rows:
        try:
            payload = json.loads(row["payload"])
        except (TypeError, ValueError):
            continue
        out.append({"type": row["event_type"],
                    "department_id": departments.get((payload.get("department") or "").lower()),
                    "source_department_id": row["source_department_id"]})
    return out


def _demo_events():
    rows = db.query(
        """SELECT event_type, department_id, source_department_id
             FROM demand_forecast_demo_events
            WHERE occurred_at >= now() - (%s * interval '1 day')""", (LOOKBACK_DAYS,))
    return [{"type": r["event_type"], "department_id": r["department_id"],
             "source_department_id": r["source_department_id"]} for r in rows]


def _mapped_resources(department_id, services):
    ids = {r["service_id"] for r in db.query(
        "SELECT service_id FROM department_access WHERE department_id=%s", (department_id,))}
    laptop = next((s for s in services if s["resource_type"] == "laptop"), None)
    account = next((s for s in services if s["resource_type"] == "account"), None)
    if laptop and account and laptop["id"] in ids:
        ids.add(account["id"])
    return ids


def build_forecast():
    """Build an advisory 90-day projection. It never changes capacity or inventory."""
    departments = db.query("SELECT id, name FROM departments ORDER BY name")
    by_name = {d["name"].lower(): d["id"] for d in departments}
    services = db.query("SELECT id, name, resource_type FROM services WHERE resource_type IN ('laptop','account') ORDER BY id")
    real, demo = _real_events(by_name), _demo_events()
    deltas = event_deltas(real + demo)
    available = {r["kind"]: r["count"] for r in db.query(
        "SELECT kind, COUNT(*) AS count FROM assets WHERE status='available' GROUP BY kind")}

    org_change = defaultdict(int)
    department_rows = []
    for dept in departments:
        net_people = deltas.get(dept["id"], 0)
        mapped = _mapped_resources(dept["id"], services)
        resources = []
        for service in services:
            current = db.query(
                """SELECT COUNT(*) AS count FROM employee_access ea JOIN employees e ON e.id=ea.employee_id
                   WHERE e.department_id=%s AND ea.service_id=%s""", (dept["id"], service["id"]), fetch="one")["count"]
            allowed = db.query("SELECT allowed FROM department_quotas WHERE department_id=%s AND service_id=%s",
                               (dept["id"], service["id"]), fetch="one")
            allowed = allowed["allowed"] if allowed else 0
            change = net_people if service["id"] in mapped else 0
            projection = resource_projection(current, change, allowed)
            org_change[service["id"]] += change
            resources.append({"service_id": service["id"], "name": service["name"],
                              "resource_type": service["resource_type"], "current_used": current,
                              "projected_change": change, "projected_demand": projection["projected_demand"],
                              "department_quota": allowed, "quota_gap": projection["quota_gap"],
                              "recommended_department_quota": projection["recommended_quota"]})
        department_rows.append({"department_id": dept["id"], "department_name": dept["name"],
                                "projected_net_headcount": net_people, "resources": resources})

    organisation = []
    for service in services:
        current = db.query("SELECT COUNT(*) AS count FROM employee_access WHERE service_id=%s", (service["id"],), fetch="one")["count"]
        allowed = db.query("SELECT allowed FROM quotas WHERE service_id=%s", (service["id"],), fetch="one")
        allowed = allowed["allowed"] if allowed else 0
        change = org_change[service["id"]]
        asset_available = available.get(ASSET_KIND[service["resource_type"]], 0)
        projection = resource_projection(current, change, allowed, asset_available)
        organisation.append({"service_id": service["id"], "name": service["name"],
                             "resource_type": service["resource_type"], "current_used": current,
                             "projected_change": change, "projected_demand": projection["projected_demand"],
                             "organisation_quota": allowed, "quota_gap": projection["quota_gap"],
                             "recommended_organisation_quota": projection["recommended_quota"],
                             "available_assets": asset_available,
                             "procurement_needed": projection["procurement_needed"]})

    return {"lookback_days": LOOKBACK_DAYS, "horizon_days": HORIZON_DAYS,
            "real_event_count": len(real), "demo_event_count": len(demo),
            "demo_data_included": bool(demo), "departments": department_rows,
            "organisation": organisation}
