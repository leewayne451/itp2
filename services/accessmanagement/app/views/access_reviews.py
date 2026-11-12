"""Server-rendered access review and reconciliation console (CHANGE 010)."""
from flask import Blueprint, Response, flash, g, redirect, render_template, request, url_for

from . import role_required
from ..backend_client import call, error_of, get_json

bp = Blueprint("access_reviews", __name__)


def _index_url(campaign_id=None):
    return url_for("access_reviews.index", campaign_id=campaign_id) if campaign_id else url_for("access_reviews.index")


@bp.get("/access-reviews")
@role_required("it_admin", "director", "euss")
def index():
    campaign_id = request.args.get("campaign_id", type=int)
    campaign = get_json(f"/api/access-reviews/{campaign_id}", default=None) if campaign_id else None
    employees = get_json("/api/employees", default=[]) if g.user["role"] == "it_admin" else []
    return render_template(
        "access_reviews.html",
        campaigns=get_json("/api/access-reviews", default=[]),
        campaign=campaign,
        departments=get_json("/api/departments", default=[]) if g.user["role"] == "it_admin" else [],
        directors=[e for e in employees if e["role"] == "director" and e["status"] == "active"],
        collection_tasks=get_json("/api/access-review-asset-tasks", params={"status": "pending"}, default=[])
                         if g.user["role"] in ("it_admin", "euss") else [],
    )


@bp.post("/access-reviews")
@role_required("it_admin")
def create():
    due = request.form.get("due_at", "").strip()
    if due and "+" not in due[10:] and not due.endswith("Z"):
        due += "+08:00"
    payload = {
        "name": request.form.get("name", "").strip(),
        "department_id": request.form.get("department_id", type=int),
        "reviewer_id": request.form.get("reviewer_id", type=int),
        "due_at": due,
    }
    resp = call("POST", "/api/access-reviews", json=payload)
    if resp.ok:
        campaign = resp.json()
        flash(f"Access review created with {campaign['summary']['total']} item(s).", "ok")
        return redirect(_index_url(campaign["id"]))
    flash(error_of(resp), "error")
    return redirect(_index_url())


@bp.post("/access-reviews/<int:campaign_id>/reconcile")
@role_required("it_admin")
def reconcile(campaign_id):
    resp = call("POST", f"/api/access-reviews/{campaign_id}/reconcile", json={})
    flash("Reconciliation completed." if resp.ok else error_of(resp), "ok" if resp.ok else "error")
    return redirect(_index_url(campaign_id))


@bp.post("/access-reviews/<int:campaign_id>/close")
@role_required("it_admin")
def close(campaign_id):
    resp = call("POST", f"/api/access-reviews/{campaign_id}/close", json={})
    flash("Access review completed." if resp.ok else error_of(resp), "ok" if resp.ok else "error")
    return redirect(_index_url(campaign_id))


@bp.post("/access-review-items/<int:item_id>/decision")
@role_required("director")
def decide(item_id):
    campaign_id = request.form.get("campaign_id", type=int)
    payload = {"decision": request.form.get("decision"), "reason": request.form.get("reason", "").strip()}
    resp = call("POST", f"/api/access-review-items/{item_id}/decision", json=payload)
    if resp.ok:
        result = resp.json()
        suffix = f" {len(result['asset_task_ids'])} asset collection task(s) created." if result["asset_task_ids"] else ""
        flash(f"Decision recorded.{suffix}", "ok")
    else:
        flash(error_of(resp), "error")
    return redirect(_index_url(campaign_id))


@bp.post("/access-review-items/<int:item_id>/resolve")
@role_required("it_admin")
def resolve(item_id):
    campaign_id = request.form.get("campaign_id", type=int)
    resp = call("POST", f"/api/access-review-items/{item_id}/resolve",
                json={"note": request.form.get("note", "").strip()})
    flash("Investigation resolved." if resp.ok else error_of(resp), "ok" if resp.ok else "error")
    return redirect(_index_url(campaign_id))


@bp.post("/access-review-asset-tasks/<int:task_id>/collect")
@role_required("euss")
def collect(task_id):
    campaign_id = request.form.get("campaign_id", type=int)
    resp = call("POST", f"/api/access-review-asset-tasks/{task_id}/collect", json={})
    flash("Asset collection recorded." if resp.ok else error_of(resp), "ok" if resp.ok else "error")
    return redirect(_index_url(campaign_id))


@bp.get("/access-reviews/<int:campaign_id>/export")
@role_required("it_admin")
def export(campaign_id):
    resp = call("GET", f"/api/access-reviews/{campaign_id}/export")
    if not resp.ok:
        flash(error_of(resp), "error")
        return redirect(_index_url(campaign_id))
    out = Response(resp.content, mimetype=resp.headers.get("Content-Type", "text/csv"))
    if "Content-Disposition" in resp.headers:
        out.headers["Content-Disposition"] = resp.headers["Content-Disposition"]
    return out

