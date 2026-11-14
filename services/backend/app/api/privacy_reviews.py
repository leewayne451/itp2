"""Manual exception workflow for documents that cannot be auto-sanitised."""
from flask import Blueprint, g, jsonify, request

from .. import document_privacy
from ..auth import require_auth, require_role
from ..core import add_event, validate_documents


bp = Blueprint("privacy_reviews_api", __name__)


def _not_found():
    return jsonify(error="Document is not available for this privacy action"), 409


@bp.get("/api/privacy-documents")
@require_role("it_admin")
def list_items():
    return jsonify(document_privacy.review_items())


@bp.get("/api/privacy-dashboard")
@require_role("it_admin")
def dashboard():
    return jsonify(document_privacy.privacy_dashboard())


@bp.post("/api/privacy-retention-policies/<outcome>")
@require_role("it_admin")
def update_policy(outcome):
    body = request.get_json(silent=True) or {}
    try:
        seconds = int(body.get("retention_seconds"))
    except (TypeError, ValueError):
        return jsonify(error="Retention seconds must be a whole number"), 400
    row = document_privacy.update_retention_policy(outcome, seconds, g.user["id"])
    if not row:
        return jsonify(error="Policy must be completed/rejected and between 60 and 315360000 seconds"), 400
    return jsonify(row)


@bp.post("/api/privacy-documents/<int:doc_id>/legal-hold")
@require_role("it_admin")
def legal_hold(doc_id):
    reason = ((request.get_json(silent=True) or {}).get("reason") or "").strip()
    if not reason or len(reason) > 500:
        return jsonify(error="A legal-hold reason of 1-500 characters is required"), 400
    row = document_privacy.apply_legal_hold(doc_id, reason, g.user["id"])
    return jsonify(ok=True) if row else _not_found()


@bp.post("/api/privacy-documents/<int:doc_id>/legal-hold/release")
@require_role("it_admin")
def release_legal_hold(doc_id):
    reason = ((request.get_json(silent=True) or {}).get("reason") or "").strip()
    if not reason or len(reason) > 500:
        return jsonify(error="A release reason of 1-500 characters is required"), 400
    row = document_privacy.release_legal_hold(doc_id, reason, g.user["id"])
    return jsonify(ok=True) if row else _not_found()


@bp.post("/api/privacy-documents/<int:doc_id>/retry")
@require_role("it_admin")
def retry(doc_id):
    row = document_privacy.retry_document(doc_id, g.user["id"])
    if not row:
        return _not_found()
    add_event(row["request_id"], g.user["id"], "document_scan_retried",
              "Privacy review requested another automated scan", notify_incoming=False,
              notify_target=False, background=True)
    return jsonify(ok=True)


@bp.post("/api/privacy-documents/<int:doc_id>/request-replacement")
@require_role("it_admin")
def request_replacement(doc_id):
    reason = ((request.get_json(silent=True) or {}).get("reason") or "").strip()
    if not reason:
        return jsonify(error="A replacement reason is required"), 400
    if len(reason) > 500:
        return jsonify(error="Replacement reason must be 500 characters or fewer"), 400
    row = document_privacy.request_replacement(doc_id, g.user["id"], reason)
    if not row:
        return _not_found()
    add_event(row["request_id"], g.user["id"], "document_replacement_requested", reason,
              notify_incoming=False, notify_target=False, background=True)
    return jsonify(ok=True)


@bp.post("/api/privacy-documents/<int:doc_id>/replace")
@require_auth
def replace(doc_id):
    if g.user["role"] not in ("it_admin", "requestor"):
        return jsonify(error="Only IT Admin or the owning Requestor can replace this document"), 403
    document = (request.get_json(silent=True) or {}).get("document") or {}
    error = validate_documents([document])
    if error:
        return jsonify(error=error), 400
    row = document_privacy.replace_document(doc_id, g.user, document)
    if not row:
        return _not_found()
    add_event(row["request_id"], g.user["id"], "document_replaced",
              "A replacement document was uploaded and quarantined for scanning",
              notify_incoming=False, notify_target=False, background=True)
    return jsonify(ok=True)


@bp.post("/api/privacy-documents/<int:doc_id>/remove")
@require_role("it_admin")
def remove(doc_id):
    reason = ((request.get_json(silent=True) or {}).get("reason") or "").strip()
    if not reason:
        return jsonify(error="A removal justification is required"), 400
    if len(reason) > 500:
        return jsonify(error="Removal justification must be 500 characters or fewer"), 400
    row = document_privacy.remove_document(doc_id, g.user["id"], reason)
    if not row:
        return _not_found()
    add_event(row["request_id"], g.user["id"], "document_removed", reason,
              notify_incoming=False, notify_target=False, background=True)
    return jsonify(ok=True)
