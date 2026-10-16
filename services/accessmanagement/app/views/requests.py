"""Raising and acting on requests (all via POST → redirect → GET)."""
import base64

from flask import Blueprint, request, redirect, url_for, flash, render_template, g

from . import login_required, role_required
from ..backend_client import call, get_json, error_of

bp = Blueprint("requests", __name__)


# PDF-only. This is an early, friendly check; the backend re-validates every upload
# authoritatively (the %PDF- magic header is the real gate). Keep these in step with
# core.validate_documents on the backend.
PDF_MAGIC = b"%PDF-"
MAX_DOC_BYTES = 5 * 1024 * 1024
MAX_DOCUMENTS = 10


def _collect_documents():
    """Read supporting uploads, enforcing PDF-only. Returns (docs, error)."""
    files = [f for f in request.files.getlist("documents") if f and f.filename]
    if len(files) > MAX_DOCUMENTS:
        return None, f"Too many documents (max {MAX_DOCUMENTS})."
    docs = []
    for f in files:
        name = f.filename
        if not name.lower().endswith(".pdf"):
            return None, f"'{name}' is not a PDF. Only PDF files are allowed."
        data = f.read()
        if not data:
            return None, f"'{name}' is empty."
        if len(data) > MAX_DOC_BYTES:
            return None, f"'{name}' exceeds the {MAX_DOC_BYTES // (1024 * 1024)} MB limit."
        if not data.startswith(PDF_MAGIC):
            return None, f"'{name}' is not a valid PDF (failed content check)."
        docs.append({"filename": name, "content_base64": base64.b64encode(data).decode("ascii")})
    return docs, None


def _back():
    return redirect(request.referrer or url_for("dashboard.index"))


@bp.post("/requests")
@role_required("requestor")
def create():
    rtype = request.form.get("type")
    documents, doc_error = _collect_documents()
    if doc_error:
        flash(doc_error, "error")
        return _back()
    payload = {
        "type": rtype,
        "target_employee_id": int(request.form["target_employee_id"]) if request.form.get("target_employee_id") else None,
        "on_behalf": request.form.get("on_behalf") == "behalf",
        "justification": request.form.get("justification", "").strip(),
        "documents": documents,
    }
    if rtype in ("onboard", "shift"):
        payload["department_id"] = int(request.form["department_id"]) if request.form.get("department_id") else None
    else:
        payload["service_id"] = int(request.form["service_id"]) if request.form.get("service_id") else None

    resp = call("POST", "/api/requests", json=payload)
    if resp.ok:
        flash("Request submitted.", "ok")
    else:
        flash(error_of(resp), "error")
    return _back()


@bp.post("/requests/<int:rid>/approve")
@role_required("hr", "director", "aa")
def approve(rid):
    resp = call("POST", f"/api/requests/{rid}/approve", json={})
    flash("Approved." if resp.ok else error_of(resp), "ok" if resp.ok else "error")
    return _back()


@bp.post("/requests/<int:rid>/reject")
@role_required("hr", "director", "aa", "it_admin")
def reject(rid):
    reason = request.form.get("reason", "").strip()
    resp = call("POST", f"/api/requests/{rid}/reject", json={"reason": reason})
    flash("Rejected — sent back to requestor." if resp.ok else error_of(resp), "ok" if resp.ok else "error")
    return _back()


@bp.post("/requests/<int:rid>/process")
@role_required("it_admin")
def process(rid):
    override = request.form.get("override_reason", "").strip()
    body = {"override_reason": override} if override else {}
    resp = call("POST", f"/api/requests/{rid}/process", json=body)
    if resp.ok:
        flash("Processed.", "ok")
    elif resp.status_code == 409 and (resp.json() or {}).get("requires_override"):
        warn = resp.json().get("warning", "Quota exceeded")
        flash(f"{warn}. To proceed, enter an override justification and process again.", "error")
    else:
        flash(error_of(resp), "error")
    return _back()


@bp.post("/requests/<int:rid>/appointment")
@role_required("requestor")
def appointment(rid):
    when = request.form.get("appointment_at", "").strip()
    if not when:
        flash("Pick a date and time for the appointment.", "error")
        return _back()
    resp = call("POST", f"/api/requests/{rid}/appointment", json={"appointment_at": when})
    flash("Appointment booked — sent to EUSS." if resp.ok else error_of(resp), "ok" if resp.ok else "error")
    return _back()


@bp.post("/requests/<int:rid>/provision")
@role_required("euss")
def provision(rid):
    # One required <select name="tag_<Kind>"> per needed kind on the EUSS page.
    asset_tags = {key[len("tag_"):]: val.strip()
                  for key, val in request.form.items() if key.startswith("tag_") and val.strip()}
    resp = call("POST", f"/api/requests/{rid}/provision", json={"asset_tags": asset_tags})
    flash("Provisioned — request completed." if resp.ok else error_of(resp), "ok" if resp.ok else "error")
    return _back()


@bp.post("/requests/<int:rid>/retrieve")
@role_required("euss")
def retrieve(rid):
    resp = call("POST", f"/api/requests/{rid}/retrieve", json={})
    flash("Asset retrieved; access revoked." if resp.ok else error_of(resp), "ok" if resp.ok else "error")
    return _back()


@bp.get("/requests/<int:rid>")
@login_required
def detail(rid):
    resp = call("GET", f"/api/requests/{rid}")
    if not resp.ok:
        flash(error_of(resp), "error")
        return redirect(url_for("dashboard.index"))
    return render_template("request_detail.html", r=resp.json())
