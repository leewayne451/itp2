"""Access review, reconciliation and EUSS collection endpoints (CHANGE 010)."""
import csv
import io
from datetime import datetime, timezone

from flask import Blueprint, Response, g, jsonify, request

from .. import access_reviews, audit
from ..auth import require_role

bp = Blueprint("access_reviews_api", __name__)


def _error(exc):
    return jsonify(error=exc.message), exc.status


@bp.get("/api/access-reviews")
@require_role("it_admin", "director", "euss")
def list_campaigns():
    return jsonify(access_reviews.campaigns_for(g.user))


@bp.post("/api/access-reviews")
@require_role("it_admin")
def create_campaign():
    body = request.get_json(silent=True) or {}
    try:
        cid = access_reviews.create_campaign(
            body.get("name"), body.get("department_id"), body.get("reviewer_id"),
            body.get("due_at"), g.user["id"])
        return jsonify(access_reviews.get_campaign(cid, g.user)), 201
    except access_reviews.ReviewError as e:
        return _error(e)


@bp.get("/api/access-reviews/<int:campaign_id>")
@require_role("it_admin", "director")
def campaign_detail(campaign_id):
    try:
        return jsonify(access_reviews.get_campaign(campaign_id, g.user))
    except access_reviews.ReviewError as e:
        return _error(e)


@bp.post("/api/access-reviews/<int:campaign_id>/reconcile")
@require_role("it_admin")
def reconcile(campaign_id):
    try:
        access_reviews.reconcile(campaign_id)
        return jsonify(access_reviews.get_campaign(campaign_id, g.user))
    except access_reviews.ReviewError as e:
        return _error(e)


@bp.post("/api/access-reviews/<int:campaign_id>/close")
@require_role("it_admin")
def close(campaign_id):
    try:
        access_reviews.close_campaign(campaign_id)
        return jsonify(access_reviews.get_campaign(campaign_id, g.user))
    except access_reviews.ReviewError as e:
        return _error(e)


@bp.post("/api/access-review-items/<int:item_id>/decision")
@require_role("director")
def decide(item_id):
    body = request.get_json(silent=True) or {}
    try:
        return jsonify(access_reviews.decide(item_id, g.user, body.get("decision"), body.get("reason")))
    except access_reviews.ReviewError as e:
        return _error(e)


@bp.post("/api/access-review-items/<int:item_id>/resolve")
@require_role("it_admin")
def resolve(item_id):
    body = request.get_json(silent=True) or {}
    try:
        access_reviews.resolve_investigation(item_id, g.user, body.get("note"))
        return jsonify(ok=True)
    except access_reviews.ReviewError as e:
        return _error(e)


@bp.get("/api/access-review-asset-tasks")
@require_role("euss", "it_admin")
def collection_tasks():
    return jsonify(access_reviews.asset_tasks(request.args.get("status")))


@bp.post("/api/access-review-asset-tasks/<int:task_id>/collect")
@require_role("euss")
def collect(task_id):
    try:
        access_reviews.collect_asset(task_id, g.user)
        return jsonify(ok=True)
    except access_reviews.ReviewError as e:
        return _error(e)


def _csv_safe(value):
    text = "" if value is None else str(value)
    return "'" + text if text[:1] in ("=", "+", "-", "@", "\t", "\r") else text


@bp.get("/api/access-reviews/<int:campaign_id>/export")
@require_role("it_admin")
def export(campaign_id):
    try:
        campaign = access_reviews.get_campaign(campaign_id, g.user)
    except access_reviews.ReviewError as e:
        return _error(e)
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["campaign", "department", "employee", "username", "status", "resource",
                     "expected", "actual", "risk", "finding", "source_request", "asset",
                     "decision", "reason", "reviewer", "reviewed_at", "remediation",
                     "return_request", "return_status"])
    for item in campaign["items"]:
        writer.writerow([_csv_safe(v) for v in (
            campaign["name"], campaign["department_name"], item["employee_name"],
            item["employee_username"], item["employee_status"], item["service_name"],
            item["expected_access"], item["actual_access"], item["risk_level"],
            "; ".join(item["finding_labels"]), item["source_request_id"], item["asset_tag"],
            item["decision"], item["decision_reason"], item["reviewed_by_name"],
            item["reviewed_at"], item["remediation_status"], item["return_request_id"],
            item["return_status"])])
    body = buf.getvalue().encode("utf-8-sig")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    audit.record("audit", "access_review_exported", target_type="access_review",
                 target_id=campaign_id, target_label=campaign["name"],
                 details={"records": len(campaign["items"])})
    response = Response(body, mimetype="text/csv")
    response.headers["Content-Disposition"] = f'attachment; filename="access-review-{campaign_id}-{stamp}.csv"'
    return response

