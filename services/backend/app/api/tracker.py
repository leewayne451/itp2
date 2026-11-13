"""New-hire journey tracker + EUSS appointment slots (CHANGE 011).

    GET  /api/tracker/me                     the signed-in employee's own requests, tracker view
    GET  /api/requests/<id>/tracker          one request's tracker (employee it is about,
                                             its requestor, staff)
    GET  /api/requests/<id>/slots            free slots it could move to
    POST /api/requests/<id>/reschedule       move the appointment  {"slot_id": 12}
    GET  /api/euss/slots?days=7              EUSS: the slot board with who is booked
    POST /api/euss/slots                     EUSS: add a one-off slot
    POST /api/euss/slots/<id>/block|reopen   EUSS: block a slot (bookings are moved) / re-open it
"""
from datetime import datetime, timezone

from flask import Blueprint, request, jsonify, g

from .. import db, scheduling
from ..auth import require_auth, require_role
from ..core import STAFF_ROLES, get_request, is_system_actor_id

bp = Blueprint("tracker_api", __name__)


def _iso(v):
    return v.isoformat() if v else None


def _may_track(req):
    """The employee the request is about, its requestor (any Requestor for one the HR
    system or an access review raised), and staff."""
    role, uid = g.user["role"], g.user["id"]
    if req["target_employee_id"] == uid or req["requestor_id"] == uid or role in STAFF_ROLES:
        return True
    return role == "requestor" and is_system_actor_id(req["requestor_id"])


def _may_move(req):
    """Who may pick another slot: the employee, the requestor, or EUSS (the desk)."""
    role, uid = g.user["role"], g.user["id"]
    if req["target_employee_id"] == uid or req["requestor_id"] == uid or role == "euss":
        return True
    return role == "requestor" and is_system_actor_id(req["requestor_id"])


def _slot(s):
    return {"id": s["id"], "starts_at": _iso(s["starts_at"]), "ends_at": _iso(s["ends_at"]),
            "location": s["location"], "free": s.get("free"), "capacity": s.get("capacity")}


@bp.get("/api/tracker/me")
@require_auth
def my_journey():
    rows = db.query("""SELECT id FROM requests WHERE target_employee_id=%s
                       ORDER BY (status IN ('completed','rejected')), created_at DESC LIMIT 10""",
                    (g.user["id"],))
    stats, owners = scheduling._stage_stats(), scheduling._owners()
    out = []
    for r in rows:
        t = scheduling.tracker(r["id"], stats=stats, owners=owners)
        t["can_move"] = t["status"] == "pending_euss"
        out.append(t)
    return jsonify(out)


@bp.get("/api/requests/<int:rid>/tracker")
@require_auth
def request_tracker(rid):
    req = get_request(rid)
    if not req:
        return jsonify(error="Request not found"), 404
    if not _may_track(req):
        return jsonify(error="Forbidden"), 403
    t = scheduling.tracker(rid)
    t["can_move"] = t["status"] == "pending_euss" and _may_move(req)
    return jsonify(t)


@bp.get("/api/requests/<int:rid>/slots")
@require_auth
def request_slots(rid):
    req = get_request(rid)
    if not req:
        return jsonify(error="Request not found"), 404
    if not _may_move(req):
        return jsonify(error="Forbidden"), 403
    limit = min(int(request.args.get("limit", 12)), 50)
    slots = scheduling.free_slots(scheduling.earliest_start(), exclude_request=rid)
    current = scheduling.live_booking(rid)
    best, reason = scheduling.find_slot(rid)
    return jsonify(slots=[_slot(s) for s in slots if not current or s["id"] != current["slot_id"]][:limit],
                   suggested=_slot(best) if best else None, suggested_reason=reason,
                   lead_hours=scheduling.LEAD_HOURS)


@bp.post("/api/requests/<int:rid>/reschedule")
@require_auth
def reschedule(rid):
    req = get_request(rid)
    if not req:
        return jsonify(error="Request not found"), 404
    if not _may_move(req):
        return jsonify(error="Only the employee, the requestor or EUSS can change this appointment"), 403
    if req["status"] != "pending_euss":
        return jsonify(error="This request has no appointment to move"), 409
    try:
        slot_id = int((request.get_json(silent=True) or {}).get("slot_id"))
    except (TypeError, ValueError):
        return jsonify(error="slot_id is required"), 400
    try:
        slot = scheduling.reschedule(rid, slot_id, g.user["id"])
    except scheduling.SlotUnavailable as e:
        return jsonify(error=str(e)), 409
    return jsonify(ok=True, slot=_slot(slot))


# --------------------------------------------------------------------------- #
# EUSS: the slot board
# --------------------------------------------------------------------------- #
@bp.get("/api/euss/slots")
@require_role("euss", "admin")
def slot_board():
    days = max(1, min(int(request.args.get("days", 7)), scheduling.HORIZON_DAYS))
    rows, by_slot = scheduling.slot_board(days)
    return jsonify(
        settings={"location": scheduling.LOCATION, "slot_minutes": scheduling.SLOT_MINUTES,
                  "desks": scheduling.DESKS, "lead_hours": scheduling.LEAD_HOURS,
                  "horizon_days": scheduling.HORIZON_DAYS, "auto_match": scheduling.AUTO_MATCH},
        slots=[dict(_slot(r), status=r["status"], source=r["source"], note=r["note"],
                    bookings=[{"request_id": b["request_id"], "type": b["type"], "target_name": b["target_name"],
                               "method": b["method"]} for b in by_slot.get(r["id"], [])])
               for r in rows])


@bp.post("/api/euss/slots")
@require_role("euss")
def add_slot():
    body = request.get_json(silent=True) or {}
    try:
        starts = datetime.fromisoformat(body["starts_at"])
        if starts.tzinfo is None:   # a time typed in the console is Singapore time
            from ..tracker_logic import SGT
            starts = starts.replace(tzinfo=SGT)
        minutes = int(body.get("minutes") or scheduling.SLOT_MINUTES)
        capacity = int(body.get("capacity") or 1)
    except (KeyError, TypeError, ValueError):
        return jsonify(error="starts_at (date and time) is required"), 400
    if not (5 <= minutes <= 240) or not (1 <= capacity <= 20):
        return jsonify(error="A slot is 5–240 minutes long with 1–20 places"), 400
    if starts <= datetime.now(timezone.utc):
        return jsonify(error="The slot must be in the future"), 400
    row = scheduling.add_slot(starts, minutes, capacity, (body.get("location") or "").strip() or None,
                              g.user["id"], (body.get("note") or "").strip() or None)
    return jsonify(_slot(row)), 201


@bp.post("/api/euss/slots/<int:slot_id>/<action>")
@require_role("euss")
def change_slot(slot_id, action):
    if action not in ("block", "reopen"):
        return jsonify(error="action must be block or reopen"), 404
    reason = ((request.get_json(silent=True) or {}).get("reason") or "").strip() or None
    if action == "block" and not reason:
        return jsonify(error="Say why the slot is blocked (e.g. staff training)"), 400
    slot, moved, unplaced = scheduling.set_slot_status(slot_id, "blocked" if action == "block" else "open",
                                                       g.user, reason)
    if not slot:
        return jsonify(error="Slot not found"), 404
    return jsonify(ok=True, moved=moved, unplaced=unplaced)
