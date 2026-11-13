"""The role-aware dashboard: one server-rendered page per role."""
from flask import Blueprint, render_template, g, redirect, url_for

from . import login_required
from ..backend_client import get_json
from ..labels import to_sgt

bp = Blueprint("dashboard", __name__)

KIND_OF = {"laptop": "Laptop", "account": "Account"}


@bp.get("/")
@login_required
def index():
    role = g.user["role"]
    if role == "requestor":
        return _requestor()
    if role in ("hr", "director", "aa"):
        return _approver()
    if role == "it_admin":
        return _it_admin()
    if role == "euss":
        return _euss()
    if role == "admin":
        return _admin()
    if role == "employee":           # new-hire journey tracker (CHANGE 011)
        return redirect(url_for("tracker.my_journey"))
    return render_template("dashboard/no_role.html")


def _requestor():
    employees = get_json("/api/employees", default=[])
    emps = [e for e in employees if e["role"] == "employee"]
    to_book = get_json("/api/requests/queue", default=[]) or []
    for r in to_book:   # free EUSS slots to pick from, plus the smart match's suggestion (CHANGE 011)
        r["slots"] = get_json(f"/api/requests/{r['id']}/slots", params={"limit": 15}, default={}) or {}
    return render_template(
        "dashboard/requestor.html",
        to_book=to_book,
        onboarded=[e for e in emps if e["department_id"]],
        unonboarded=[e for e in emps if not e["department_id"]],
        services=get_json("/api/services", default=[]),
        departments=get_json("/api/departments", default=[]),
        mine=get_json("/api/requests/mine", default=[]),
        planned_rotations=get_json("/api/rotations/planned", default=[]),
    )


def _approver():
    role = g.user["role"]
    is_aa = role == "aa"
    # AA sees the quota picture (org-wide + per department) to inform their decision.
    # Department quotas are read-only here — only IT Admin may change them.
    quotas = get_json("/api/quotas", default=[]) if is_aa else []
    department_quotas = get_json("/api/department-quotas", default=[]) if is_aa else []
    quota_reqs = get_json("/api/quota-requests", default=[]) if is_aa else []
    mine_qr = [q for q in quota_reqs if q["requested_by"] == g.user["id"]]
    # Mandatory Job Rotation (CHANGE 006): Directors decide, HR follows the list.
    mjr = get_json("/api/rotations", default=None) if role in ("director", "hr") else None
    officers = (mjr or {}).get("officers", [])
    return render_template(
        "dashboard/approver.html",
        queue=get_json("/api/requests/queue", default=[]),
        history=get_json("/api/requests/history", default=[]),
        quotas=quotas, department_quotas=department_quotas, my_quota_requests=mine_qr,
        mjr=mjr,
        mjr_due=[o for o in officers if o["state"] == "due"],
        mjr_soon=[o for o in officers if o["state"] == "due_soon"],
        mjr_tracked=[o for o in officers if o["state"] in ("rotation_planned", "shift_in_progress", "kept")],
        mjr_all=officers,
        departments=get_json("/api/departments", default=[]) if role == "director" else [],
    )


def _it_admin():
    return render_template(
        "dashboard/it_admin.html",
        queue=get_json("/api/requests/queue", default=[]),
        history=get_json("/api/requests/history", default=[]),
        services=get_json("/api/services", default=[]),
        departments=get_json("/api/departments", default=[]),
        quotas=get_json("/api/quotas", default=[]),
        quota_requests=get_json("/api/quota-requests", default=[]),
        department_quotas=get_json("/api/department-quotas", default=[]),
    )


def _provision_plan(req, employees_by_id, services_by_id, assets):
    """Kinds the target still needs an asset for, plus available tags per kind."""
    target = employees_by_id.get(req["target_employee_id"])
    if not target:
        return []
    needed = {KIND_OF[services_by_id[sid]["resource_type"]]
              for sid in target.get("access", []) if sid in services_by_id
              and services_by_id[sid]["resource_type"] in KIND_OF}
    held = {a["kind"] for a in assets if a["assigned_to"] == target["id"] and a["status"] == "assigned"}
    plan = []
    for kind in sorted(needed - held):
        avail = [a["asset_tag"] for a in assets if a["kind"] == kind and a["status"] == "available"]
        plan.append({"kind": kind, "available": avail})
    return plan


def _euss():
    queue = get_json("/api/requests/queue", default=[])
    employees = get_json("/api/employees", default=[])
    services = get_json("/api/services", default=[])
    assets = get_json("/api/assets", default=[])
    employees_by_id = {e["id"]: e for e in employees}
    services_by_id = {s["id"]: s for s in services}

    provisions, returns = [], []
    for r in queue:
        if r["type"] == "return":
            returns.append(r)
        else:
            r = dict(r)
            r["plan"] = _provision_plan(r, employees_by_id, services_by_id, assets)
            provisions.append(r)
    by_appt = lambda r: r.get("appointment_at") or "9999"   # noqa: E731 — soonest appointment first
    board = get_json("/api/euss/slots", params={"days": 7}, default={}) or {}
    for s in board.get("slots", []):
        local = to_sgt(s["starts_at"])
        s["day"] = local.strftime("%Y-%m-%d")          # sortable group key
        s["day_label"] = local.strftime("%a %d %b")
    return render_template(
        "dashboard/euss.html",
        provisions=sorted(provisions, key=by_appt), returns=sorted(returns, key=by_appt), assets=assets,
        slot_board=board,
        history=get_json("/api/requests/history", default=[]),
    )


def _admin():
    return render_template(
        "dashboard/admin.html",
        employees=get_json("/api/employees", default=[]),
        departments=get_json("/api/departments", default=[]),
        services=get_json("/api/services", default=[]),
        all_requests=get_json("/api/requests", default=[]),
    )
