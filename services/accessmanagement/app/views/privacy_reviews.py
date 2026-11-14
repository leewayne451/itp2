"""IT Admin document-privacy exception queue and replacement handling."""
from flask import Blueprint, flash, redirect, render_template, request, url_for

from . import role_required
from .requests import _collect_documents
from ..backend_client import call, error_of, get_json


bp = Blueprint("privacy_reviews", __name__)


def _back():
    return redirect(request.referrer or url_for("privacy_reviews.index"))


@bp.get("/privacy-reviews")
@role_required("it_admin")
def index():
    dashboard = get_json("/api/privacy-dashboard", default={})
    return render_template("privacy_reviews.html",
                           items=get_json("/api/privacy-documents", default=[]),
                           stats=dashboard.get("stats", {}),
                           policies=dashboard.get("policies", []),
                           retention=dashboard.get("retention", []))


@bp.post("/privacy-retention-policies/<outcome>")
@role_required("it_admin")
def update_policy(outcome):
    resp = call("POST", f"/api/privacy-retention-policies/{outcome}",
                json={"retention_seconds": request.form.get("retention_seconds")})
    flash("Retention policy updated." if resp.ok else error_of(resp),
          "ok" if resp.ok else "error")
    return _back()


@bp.post("/privacy-documents/<int:doc_id>/legal-hold")
@role_required("it_admin")
def legal_hold(doc_id):
    resp = call("POST", f"/api/privacy-documents/{doc_id}/legal-hold",
                json={"reason": request.form.get("reason", "").strip()})
    flash("Legal hold applied; automatic purge is suspended." if resp.ok else error_of(resp),
          "ok" if resp.ok else "error")
    return _back()


@bp.post("/privacy-documents/<int:doc_id>/legal-hold/release")
@role_required("it_admin")
def release_legal_hold(doc_id):
    resp = call("POST", f"/api/privacy-documents/{doc_id}/legal-hold/release",
                json={"reason": request.form.get("reason", "").strip()})
    flash("Legal hold released; an overdue document will purge on the next worker check."
          if resp.ok else error_of(resp), "ok" if resp.ok else "error")
    return _back()


@bp.post("/privacy-documents/<int:doc_id>/retry")
@role_required("it_admin")
def retry(doc_id):
    resp = call("POST", f"/api/privacy-documents/{doc_id}/retry", json={})
    flash("Document returned to the protected scan queue." if resp.ok else error_of(resp),
          "ok" if resp.ok else "error")
    return _back()


@bp.post("/privacy-documents/<int:doc_id>/request-replacement")
@role_required("it_admin")
def request_replacement(doc_id):
    resp = call("POST", f"/api/privacy-documents/{doc_id}/request-replacement",
                json={"reason": request.form.get("reason", "").strip()})
    flash("Replacement requested from the Requestor." if resp.ok else error_of(resp),
          "ok" if resp.ok else "error")
    return _back()


@bp.post("/privacy-documents/<int:doc_id>/replace")
@role_required("it_admin", "requestor")
def replace(doc_id):
    documents, error = _collect_documents()
    if error:
        flash(error, "error")
        return _back()
    if len(documents) != 1:
        flash("Choose exactly one replacement PDF.", "error")
        return _back()
    resp = call("POST", f"/api/privacy-documents/{doc_id}/replace",
                json={"document": documents[0]})
    flash("Replacement uploaded and quarantined for scanning." if resp.ok else error_of(resp),
          "ok" if resp.ok else "error")
    return _back()


@bp.post("/privacy-documents/<int:doc_id>/remove")
@role_required("it_admin")
def remove(doc_id):
    resp = call("POST", f"/api/privacy-documents/{doc_id}/remove",
                json={"reason": request.form.get("reason", "").strip()})
    flash("Unsafe copy removed. A valid replacement is still required before approval."
          if resp.ok else error_of(resp),
          "ok" if resp.ok else "error")
    return _back()
