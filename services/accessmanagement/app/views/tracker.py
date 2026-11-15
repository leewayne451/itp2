"""New-hire journey tracker + EUSS appointment slots (CHANGE 011).

  /my-journey                the signed-in employee's requests, tracked like a parcel
  /requests/<id>/track       one request's tracker (employee, requestor, staff)
  /requests/<id>/reschedule  pick another free EUSS slot
  /euss/slots ...            EUSS: add a one-off slot, block / re-open a slot
"""
from flask import Blueprint, render_template, request, redirect, url_for, flash

from . import login_required, role_required
from ..backend_client import call, get_json, error_of

bp = Blueprint("tracker", __name__)


def _with_slots(t):
    """Attach the free slots a booked appointment could move to."""
    if t.get("can_move"):
        t["alternatives"] = (get_json(f"/api/requests/{t['request_id']}/slots",
                                      params={"limit": 10}, default={}) or {}).get("slots", [])
    return t


def _back(default):
    return redirect(request.referrer or default)


@bp.get("/my-journey")
@login_required
def my_journey():
    journeys = [_with_slots(t) for t in get_json("/api/tracker/me", default=[]) or []]
    return render_template("my_journey.html", journeys=journeys)


@bp.get("/requests/<int:rid>/track")
@login_required
def track(rid):
    resp = call("GET", f"/api/requests/{rid}/tracker")
    if not resp.ok:
        flash(error_of(resp), "error")
        return redirect(url_for("dashboard.index"))
    return render_template("track.html", t=_with_slots(resp.json()))


@bp.post("/requests/<int:rid>/reschedule")
@login_required
def reschedule(rid):
    slot_id = request.form.get("slot_id", "").strip()
    if not slot_id:
        flash("Pick one of the free slots.", "error")
        return _back(url_for("tracker.track", rid=rid))
    resp = call("POST", f"/api/requests/{rid}/reschedule", json={"slot_id": int(slot_id)})
    flash("Appointment moved. A confirmation is on its way by e-mail." if resp.ok else error_of(resp),
          "ok" if resp.ok else "error")
    return _back(url_for("tracker.track", rid=rid))


@bp.post("/euss/slots")
@role_required("euss")
def add_slot():
    body = {"starts_at": request.form.get("starts_at", "").strip(),
            "minutes": request.form.get("minutes") or None,
            "capacity": request.form.get("capacity") or None,
            "note": request.form.get("note", "").strip()}
    resp = call("POST", "/api/euss/slots", json=body)
    flash("Slot added." if resp.ok else error_of(resp), "ok" if resp.ok else "error")
    return _back(url_for("dashboard.index"))


@bp.post("/euss/slots/<int:slot_id>/<action>")
@role_required("euss")
def change_slot(slot_id, action):
    if action not in ("block", "reopen"):
        return redirect(url_for("dashboard.index"))
    resp = call("POST", f"/api/euss/slots/{slot_id}/{action}", json={"reason": request.form.get("reason", "").strip()})
    if not resp.ok:
        flash(error_of(resp), "error")
    elif action == "block":
        j = resp.json()
        msg = "Slot blocked."
        if j.get("moved"):
            msg += f" Moved {len(j['moved'])} appointment(s) to the next free slot and told the employees."
        if j.get("unplaced"):
            msg += (f" No free slot for request(s) {', '.join('#' + str(r) for r in j['unplaced'])}:"
                    " they are back with the requestor to book.")
        flash(msg, "ok")
    else:
        flash("Slot re-opened.", "ok")
    return _back(url_for("dashboard.index"))
