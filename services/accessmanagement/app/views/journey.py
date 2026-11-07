"""Service journey analytics page (CHANGE 007): where requests get stuck, which open
requests are at risk of missing their target turnaround, and when they will
probably finish. Computed by the `analytics` container; read through the backend."""
from flask import Blueprint, render_template, request, redirect, url_for, flash

from . import role_required
from ..backend_client import call, error_of

bp = Blueprint("journey", __name__)
VIEWERS = ("hr", "director", "aa", "it_admin", "euss", "admin")

STAGE_LABEL = {
    "pending_hr": "HR approval",
    "pending_director": "Director approval",
    "pending_aa": "Approving Authority approval",
    "pending_it": "IT processing",
    "pending_appointment": "Requestor books appointment",
    "scheduled_wait": "Waiting for appointment date",
    "pending_euss": "EUSS hand-over",
}
STAGE_OWNER = {
    "pending_hr": "HR", "pending_director": "Director", "pending_aa": "Approving Authority",
    "pending_it": "IT Admin", "pending_appointment": "Requestor", "scheduled_wait": "—",
    "pending_euss": "EUSS",
}
FLAG = {   # label, pill colour
    "breached": ("Past target", "bad"),
    "at_risk": ("At risk", "bad"),
    "stuck": ("Stuck at stage", "wait"),
    "on_track": ("On track", "ok"),
}


@bp.app_template_filter("hrs")
def fmt_hours(h):
    """31.2 -> '31h', 75 -> '3.1d'. Durations under two days read better in hours."""
    if h is None:
        return "—"
    h = float(h)
    if h < 1:
        return f"{round(h * 60)}m"
    if h < 48:
        return f"{h:.0f}h"
    return f"{h / 24:.1f}d"


@bp.app_template_filter("pct")
def fmt_pct(v, digits=0):
    return "—" if v is None else f"{100 * float(v):.{digits}f}%"


@bp.get("/journey")
@role_required(*VIEWERS)
def index():
    rtype = request.args.get("type", "all")
    show = request.args.get("show", "flagged")
    resp = call("GET", "/api/journey", params={"type": rtype})
    if not resp.ok:
        flash(error_of(resp), "error")
        return redirect(url_for("dashboard.index"))
    data = resp.json()
    preds = data.get("predictions", [])
    counts = {k: sum(1 for p in preds if p["flag"] == k) for k in FLAG}
    shown = preds if show == "all" else [p for p in preds if p["flag"] != "on_track"]
    max_share = max([s["share_pct"] or 0 for s in data.get("stages", [])] + [1])
    return render_template("journey.html", data=data, run=data.get("run"), stages=data.get("stages", []),
                           preds=shown, counts=counts, total_open=len(preds), show=show, rtype=rtype,
                           max_share=max_share, STAGE_LABEL=STAGE_LABEL, STAGE_OWNER=STAGE_OWNER, FLAG=FLAG)


@bp.post("/journey/refresh")
@role_required(*VIEWERS)
def refresh():
    resp = call("POST", "/api/journey/refresh", json={})
    flash("Re-run requested — the figures update within a few seconds. Reload the page to see them."
          if resp.ok else error_of(resp), "ok" if resp.ok else "error")
    return redirect(url_for("journey.index", **request.args))
