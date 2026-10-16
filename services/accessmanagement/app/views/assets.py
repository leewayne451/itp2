"""Asset chain-of-custody page and document downloads."""
import base64

from flask import Blueprint, render_template, redirect, url_for, flash, Response
from werkzeug.utils import secure_filename

from . import login_required, role_required
from ..backend_client import call, get_json, error_of

bp = Blueprint("assets", __name__)


@bp.get("/assets/<int:asset_id>/history")
@role_required("euss", "admin")
def history(asset_id):
    data = get_json(f"/api/assets/{asset_id}/history")
    if not data:
        flash("Could not load asset history.", "error")
        return redirect(url_for("dashboard.index"))
    return render_template("asset_history.html", asset=data["asset"], events=data["events"])


@bp.get("/documents/<int:doc_id>")
@login_required
def download(doc_id):
    resp = call("GET", f"/api/documents/{doc_id}")
    if not resp.ok:
        flash(error_of(resp), "error")
        return redirect(url_for("dashboard.index"))
    data = resp.json()
    content = base64.b64decode(data["content_base64"])
    # Sanitise the stored filename before it goes into a response header (it was
    # user-supplied at upload) to prevent header injection / path tricks. Always
    # served as an attachment so the browser never renders it inline.
    safe_name = secure_filename(data.get("filename") or "") or "document.pdf"
    return Response(content, headers={
        "Content-Disposition": f'attachment; filename="{safe_name}"',
        "Content-Type": "application/pdf",
    })
