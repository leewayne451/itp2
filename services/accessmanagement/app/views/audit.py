"""IT administrators' window onto the audit trail: search, integrity check and
download. Read-only — the backend offers no way to edit or delete entries."""
from flask import Blueprint, render_template, request, flash, redirect, url_for, Response

from . import role_required
from ..backend_client import call, error_of

bp = Blueprint("audit", __name__)
FILTERS = ("category", "outcome", "action", "actor", "target", "q", "since", "until")


def _filters():
    return {k: request.args.get(k, "").strip() for k in FILTERS if request.args.get(k, "").strip()}


@bp.get("/audit")
@role_required("it_admin")
def index():
    filters = _filters()
    page = request.args.get("page", "1")
    resp = call("GET", "/api/audit", params=dict(filters, page=page, page_size=50))
    if not resp.ok:
        flash(error_of(resp), "error")
        return redirect(url_for("dashboard.index"))
    data = resp.json()
    pages = max(1, -(-data["total"] // data["page_size"]))
    return render_template("audit.html", data=data, filters=filters, pages=pages, report=None)


@bp.post("/audit/verify")
@role_required("it_admin")
def verify():
    resp = call("GET", "/api/audit/verify")
    if not resp.ok:
        flash(error_of(resp), "error")
        return redirect(url_for("audit.index"))
    listing = call("GET", "/api/audit", params={"page": 1, "page_size": 50}).json()
    pages = max(1, -(-listing["total"] // listing["page_size"]))
    return render_template("audit.html", data=listing, filters={}, pages=pages, report=resp.json())


@bp.get("/audit/export")
@role_required("it_admin")
def export():
    fmt = request.args.get("format", "csv")
    resp = call("GET", "/api/audit/export", params=dict(_filters(), format=fmt))
    if not resp.ok:
        flash(error_of(resp), "error")
        return redirect(url_for("audit.index"))
    out = Response(resp.content, mimetype=resp.headers.get("Content-Type", "application/octet-stream"))
    for h in ("Content-Disposition", "X-Audit-Records", "X-Audit-SHA256"):
        if h in resp.headers:
            out.headers[h] = resp.headers[h]
    return out
