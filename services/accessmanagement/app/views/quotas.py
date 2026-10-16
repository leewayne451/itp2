"""AA proposes quota changes; IT Admin approves or rejects them."""
from flask import Blueprint, request, redirect, url_for, flash

from . import role_required
from ..backend_client import call, error_of

bp = Blueprint("quotas", __name__)


def _back():
    return redirect(request.referrer or url_for("dashboard.index"))


@bp.post("/quota-requests")
@role_required("aa")
def create():
    payload = {
        "service_id": int(request.form["service_id"]) if request.form.get("service_id") else None,
        "new_allowed": request.form.get("new_allowed"),
        "reason": request.form.get("reason", "").strip(),
    }
    resp = call("POST", "/api/quota-requests", json=payload)
    flash("Quota change requested — pending IT Admin approval." if resp.ok else error_of(resp),
          "ok" if resp.ok else "error")
    return _back()


@bp.post("/quota-requests/<int:qid>/approve")
@role_required("it_admin")
def approve(qid):
    resp = call("POST", f"/api/quota-requests/{qid}/approve", json={"note": request.form.get("note", "").strip()})
    flash("Quota updated." if resp.ok else error_of(resp), "ok" if resp.ok else "error")
    return _back()


@bp.post("/quota-requests/<int:qid>/reject")
@role_required("it_admin")
def reject(qid):
    resp = call("POST", f"/api/quota-requests/{qid}/reject", json={"note": request.form.get("note", "").strip()})
    flash("Quota change rejected." if resp.ok else error_of(resp), "ok" if resp.ok else "error")
    return _back()
