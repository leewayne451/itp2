"""Smart EUSS appointment booking and the journey tracker's data (CHANGE 011).

When IT processes a request it reaches the appointment step. Instead of waiting for
the requestor to type a date, the scheduler books the first free EUSS slot that
leaves EUSS enough time to prepare the equipment, and tells the employee when to come,
where, and what to bring. The employee (or requestor) can move it to another free
slot from the tracker page; if EUSS blocks a slot, everyone in it is moved to the next
free one. Nobody has to e-mail back and forth to find a time.

Slots come from the EUSS roster (opening hours below), generated a few weeks ahead.
The pure rules (which slot, what to bring, the tracker steps) are in tracker_logic.py.
"""
import os
import threading
import time
from datetime import datetime, timedelta, timezone

from . import audit, db
from .config import CONSOLE_URL
from .core import STATE_ROLE, get_joined, is_system_actor_id, add_event, system_actor
from .mailer import send_email
from . import tracker_logic as tl

# ---- the EUSS roster (backend environment, all optional) ---------------------- #
LOCATION = os.environ.get("EUSS_LOCATION", "EUSS Service Desk, Level 1")
HOURS = tl.parse_hours(os.environ.get("EUSS_HOURS", "09:00-12:00,14:00-17:00"))
DAYS = tl.parse_days(os.environ.get("EUSS_DAYS", "0,1,2,3,4"))           # Monday = 0
SLOT_MINUTES = int(os.environ.get("EUSS_SLOT_MINUTES", "30"))
DESKS = int(os.environ.get("EUSS_DESKS", "2"))                             # people served at once
HORIZON_DAYS = int(os.environ.get("EUSS_HORIZON_DAYS", "21"))              # how far ahead slots exist
LEAD_HOURS = float(os.environ.get("EUSS_LEAD_HOURS", "24"))                # time to prepare equipment
AUTO_MATCH = os.environ.get("APPOINTMENT_AUTO_MATCH", "true").strip().lower() not in ("0", "false", "no", "off")

SCHEDULER_USERNAME = "appointment-scheduler"
_ROSTER_EVERY = 600                     # seconds between roster top-ups
_roster = {"at": 0.0}
_roster_lock = threading.Lock()


class SlotUnavailable(Exception):
    """The slot is full, blocked, in the past or too soon to prepare for."""


def scheduler_actor():
    return system_actor(SCHEDULER_USERNAME)


def _now():
    return datetime.now(timezone.utc)


def earliest_start(now=None):
    return (now or _now()) + timedelta(hours=LEAD_HOURS)


# --------------------------------------------------------------------------- #
# Slots
# --------------------------------------------------------------------------- #
def ensure_roster(force=False):
    """Make sure the roster's slots exist for the next HORIZON_DAYS. Idempotent: an
    existing slot (including one EUSS blocked) is left exactly as it is."""
    with _roster_lock:
        if not force and time.time() - _roster["at"] < _ROSTER_EVERY:
            return 0
        today = _now().astimezone(tl.SGT).date()
        slots = tl.roster_slots(today, HORIZON_DAYS, HOURS, DAYS, SLOT_MINUTES)
        added = 0
        with db.transaction() as cur:
            for start, end in slots:
                cur.execute("""INSERT INTO euss_slots (starts_at, ends_at, location, capacity, source)
                               VALUES (%s,%s,%s,%s,'roster') ON CONFLICT (starts_at, location) DO NOTHING""",
                            (start, end, LOCATION, DESKS))
                added += cur.rowcount
        _roster["at"] = time.time()
        return added


# Places taken in a slot = live bookings of requests still waiting for EUSS. A request
# that was closed some other way (e.g. by the HR kill switch) frees its place at once.
_FREE_SQL = """
    SELECT s.id, s.starts_at, s.ends_at, s.location, s.capacity, s.status, s.source, s.note,
           s.capacity - COUNT(b.id) FILTER (WHERE r.status = 'pending_euss' AND b.request_id <> %s) AS free
    FROM euss_slots s
    LEFT JOIN euss_bookings b ON b.slot_id = s.id AND b.status = 'booked'
    LEFT JOIN requests r ON r.id = b.request_id
    WHERE s.starts_at >= %s AND s.starts_at < %s {extra}
    GROUP BY s.id ORDER BY s.starts_at, s.id
"""


def free_slots(earliest, until=None, exclude_request=-1):
    """Open slots with at least one free place, starting in [earliest, until)."""
    ensure_roster()
    until = until or (_now() + timedelta(days=HORIZON_DAYS + 1))
    rows = db.query(_FREE_SQL.format(extra="AND s.status = 'open'"), (exclude_request, earliest, until))
    return [r for r in rows if r["free"] > 0]


def other_visits(target_id, exclude_request):
    """The employee's other live appointments (to avoid clashes and bundle visits)."""
    rows = db.query("""SELECT s.starts_at, s.ends_at FROM euss_bookings b
                       JOIN euss_slots s ON s.id = b.slot_id JOIN requests r ON r.id = b.request_id
                       WHERE b.status = 'booked' AND r.status = 'pending_euss'
                         AND r.target_employee_id = %s AND r.id <> %s""", (target_id, exclude_request))
    return [(r["starts_at"], r["ends_at"]) for r in rows]


def live_booking(rid, include_done=False):
    statuses = ("booked", "done") if include_done else ("booked",)
    return db.query("""SELECT b.*, s.starts_at, s.ends_at, s.location FROM euss_bookings b
                       JOIN euss_slots s ON s.id = b.slot_id
                       WHERE b.request_id = %s AND b.status = ANY(%s)
                       ORDER BY b.id DESC LIMIT 1""", (rid, list(statuses)), fetch="one")


def _book(rid, slot_id, method, actor_id, reason, allowed_statuses=("pending_appointment", "pending_euss")):
    """Give request `rid` a place in slot `slot_id`, atomically: the slot row is locked,
    so two requests can never take the last place at the same time. Any earlier booking
    of the request is kept as 'moved'. Returns the slot row (with the old one, if any)."""
    with db.transaction() as cur:
        cur.execute("SELECT id, status FROM requests WHERE id=%s FOR UPDATE", (rid,))
        req = cur.fetchone()
        if not req or req["status"] not in allowed_statuses:
            raise SlotUnavailable("This request is not at the appointment stage any more.")
        cur.execute("SELECT * FROM euss_slots WHERE id=%s FOR UPDATE", (slot_id,))
        slot = cur.fetchone()
        if not slot or slot["status"] != "open":
            raise SlotUnavailable("That slot is not open.")
        soonest = earliest_start() if method == "chosen" else _now() + timedelta(hours=1)
        if slot["starts_at"] < soonest:
            raise SlotUnavailable(f"That slot is too soon: EUSS needs {LEAD_HOURS:g} hours to get ready.")
        cur.execute("""SELECT COUNT(*) AS n FROM euss_bookings b JOIN requests r ON r.id = b.request_id
                       WHERE b.slot_id=%s AND b.status='booked' AND r.status='pending_euss' AND b.request_id<>%s""",
                    (slot_id, rid))
        if cur.fetchone()["n"] >= slot["capacity"]:
            raise SlotUnavailable("That slot has just been taken.")
        cur.execute("SELECT slot_id FROM euss_bookings WHERE request_id=%s AND status='booked'", (rid,))
        held = cur.fetchone()
        if held and held["slot_id"] == slot_id:
            raise SlotUnavailable("The appointment is already in that slot.")
        cur.execute("""UPDATE euss_bookings b SET status='moved', ended_at=now() FROM euss_slots s
                       WHERE b.request_id=%s AND b.status='booked' AND s.id=b.slot_id
                       RETURNING s.starts_at, s.ends_at, s.location, s.id""", (rid,))
        old = cur.fetchone()
        cur.execute("""INSERT INTO euss_bookings (slot_id, request_id, method, reason, booked_by)
                       VALUES (%s,%s,%s,%s,%s)""", (slot_id, rid, method, reason, actor_id))
        cur.execute("""UPDATE requests SET appointment_at=%s, status='pending_euss', updated_at=now()
                       WHERE id=%s""", (slot["starts_at"], rid))
    slot = dict(slot)
    slot["previous"] = old
    return slot


def find_slot(rid, now=None, exclude_slot=None):
    """The slot the smart match would give this request now: (slot, reason)."""
    req = get_joined(rid)
    slots = [s for s in free_slots(earliest_start(now), exclude_request=rid) if s["id"] != exclude_slot]
    return tl.choose_slot(slots, earliest_start(now), other_visits(req["target_employee_id"], rid))


def try_auto_book(rid, method="auto", exclude_slot=None):
    """Book the best free slot for a request that has just reached the appointment step.
    Returns the booked slot (with 'reason'), or None when no slot is free or the feature
    is off: the request then stays with the requestor to book by hand, as before."""
    if not AUTO_MATCH and method == "auto":
        return None
    actor = scheduler_actor()
    tried = set()
    for _ in range(5):                       # another booking may take a place first
        try:
            slot, reason = find_slot(rid, exclude_slot=exclude_slot)
        except Exception as e:  # noqa: BLE001 — never break IT processing over a booking
            print(f"[scheduling] slot search failed for #{rid}: {e}", flush=True)
            return None
        if not slot or slot["id"] in tried:
            break
        tried.add(slot["id"])
        try:
            booked = _book(rid, slot["id"], method, actor and actor["id"], reason)
            booked["reason"] = reason
            return booked
        except SlotUnavailable:
            continue
    audit.record("workflow", "appointment_auto_match_failed", outcome="failure", target_type="request",
                 target_id=rid, target_label=f"#{rid}", details={"reason": "no free EUSS slot"})
    return None


def _when(slot):
    s, e = slot["starts_at"].astimezone(tl.SGT), slot["ends_at"].astimezone(tl.SGT)
    return f"{s:%a %d %b %Y, %H:%M}–{e:%H:%M} (SGT)"


def announce(rid, slot, action="appointment_booked", actor_id=None, note_prefix=None):
    """Record the booking on the request's timeline and send the employee their
    appointment (when, where, what to bring). E-mails go out in the background so a
    slow mail server never holds up the click.

    The employee gets ONE e-mail per booking or move (the appointment e-mail, not also the
    generic progress one). EUSS is told "action required" only on the first booking,
    not again every time the time changes."""
    actor = scheduler_actor()
    actor_id = actor_id or (actor and actor["id"])
    note = f"{note_prefix + ' ' if note_prefix else ''}EUSS appointment {_when(slot)} at {slot['location']}"
    if slot.get("reason"):
        note += f" — {slot['reason']}"
    add_event(rid, actor_id, action, note, notify_incoming=(action == "appointment_booked"),
              notify_target=False, background=True)
    threading.Thread(target=_appointment_email_quietly, args=(rid, slot), daemon=True).start()


def _appointment_email_quietly(rid, slot):
    try:
        send_appointment_email(rid, slot)
    except Exception as e:  # noqa: BLE001 — e-mail is best-effort
        print(f"[scheduling] appointment e-mail for #{rid} failed: {e}", flush=True)


def send_appointment_email(rid, slot):
    r = get_joined(rid)
    emp = db.query("SELECT full_name, email FROM employees WHERE id=%s", (r["target_employee_id"],), fetch="one")
    if not emp or not emp["email"]:
        return
    svc = db.query("SELECT resource_type FROM services WHERE id=%s", (r["service_id"],), fetch="one") \
        if r["service_id"] else None
    items = tl.what_to_bring(r["type"], svc and svc["resource_type"], rid)
    body = (f"Hello {emp['full_name']},\n\n"
            f"Your EUSS appointment for request #{rid} is booked:\n\n"
            f"  When:  {_when(slot)}\n  Where: {slot['location']}\n\n"
            "Please bring:\n" + "".join(f"  - {i}\n" for i in items) +
            f"\nTrack every step of your request: {CONSOLE_URL}/my-journey\n"
            "Need a different time? Pick another free slot on that page; no need to e-mail anyone.\n")
    send_email(emp["email"], f"[ServiceJourney] Your EUSS appointment — {_when(slot)}", body)


def reschedule(rid, slot_id, actor_id):
    """Move a booked appointment to another free slot the person picked."""
    slot = _book(rid, slot_id, "chosen", actor_id, "chosen from the free slots",
                 allowed_statuses=("pending_euss", "pending_appointment"))
    prev = slot.get("previous")
    announce(rid, slot, action="appointment_rescheduled" if prev else "appointment_booked", actor_id=actor_id,
             note_prefix=f"Moved from {_when(prev)}." if prev else None)
    return slot


def close_booking(rid):
    """EUSS finished the hand-over: the place is used, not free."""
    db.query("UPDATE euss_bookings SET status='done', ended_at=now() WHERE request_id=%s AND status='booked'",
             (rid,), fetch=None)


def set_slot_status(slot_id, status, actor, reason=None):
    """EUSS blocks or re-opens a slot. Blocking moves everyone booked in it to the next
    free slot (and tells them); a request with no free slot left goes back to the
    requestor's appointment queue. Returns (slot, moved, unplaced)."""
    slot = db.query("UPDATE euss_slots SET status=%s, note=%s WHERE id=%s RETURNING *",
                    (status, reason, slot_id), fetch="one")
    if not slot:
        return None, [], []
    audit.record("workflow", f"euss_slot_{'blocked' if status == 'blocked' else 'reopened'}",
                 target_type="euss_slot", target_id=slot_id, target_label=_when(slot),
                 details={"reason": reason})
    moved, unplaced = [], []
    if status != "blocked":
        return slot, moved, unplaced
    rows = db.query("""SELECT b.request_id FROM euss_bookings b JOIN requests r ON r.id = b.request_id
                       WHERE b.slot_id=%s AND b.status='booked' AND r.status='pending_euss'
                       ORDER BY b.id""", (slot_id,))
    for row in rows:
        rid = row["request_id"]
        new = try_auto_book(rid, method="rematched", exclude_slot=slot_id)
        if new:
            new["reason"] = "your earlier slot was cancelled by EUSS; this is the next free one"
            announce(rid, new, action="appointment_rescheduled", note_prefix=f"Moved from {_when(slot)}.")
            moved.append(rid)
        else:
            db.query("UPDATE euss_bookings SET status='moved', ended_at=now() WHERE request_id=%s AND status='booked'",
                     (rid,), fetch=None)
            db.query("UPDATE requests SET status='pending_appointment', appointment_at=NULL, updated_at=now() "
                     "WHERE id=%s", (rid,), fetch=None)
            actor_row = scheduler_actor()
            add_event(rid, actor_row and actor_row["id"], "appointment_cancelled",
                      f"EUSS cancelled the slot {_when(slot)} and no other slot is free; please book a new time.")
            unplaced.append(rid)
    return slot, moved, unplaced


def add_slot(starts_at, minutes, capacity, location, actor_id, note=None):
    ends = starts_at + timedelta(minutes=minutes)
    row = db.query("""INSERT INTO euss_slots (starts_at, ends_at, location, capacity, source, note, created_by)
                      VALUES (%s,%s,%s,%s,'manual',%s,%s)
                      ON CONFLICT (starts_at, location) DO UPDATE
                        SET capacity = euss_slots.capacity + EXCLUDED.capacity, status = 'open'
                      RETURNING *""", (starts_at, ends, location or LOCATION, capacity, note, actor_id), fetch="one")
    audit.record("workflow", "euss_slot_added", target_type="euss_slot", target_id=row["id"],
                 target_label=_when(row), details={"capacity": capacity, "location": row["location"]})
    return row


def slot_board(days=7):
    """EUSS's view: every slot in the next `days` days with who is booked into it."""
    ensure_roster()
    start = _now() - timedelta(hours=2)
    rows = db.query(_FREE_SQL.format(extra=""), (-1, start, start + timedelta(days=days)))
    bookings = db.query("""SELECT b.slot_id, b.request_id, b.method, r.type, t.full_name AS target_name
                           FROM euss_bookings b JOIN requests r ON r.id = b.request_id
                           JOIN employees t ON t.id = r.target_employee_id
                           WHERE b.status='booked' AND r.status='pending_euss' AND b.slot_id = ANY(%s)
                           ORDER BY b.id""", ([r["id"] for r in rows] or [0],))
    by_slot = {}
    for b in bookings:
        by_slot.setdefault(b["slot_id"], []).append(b)
    return rows, by_slot


# --------------------------------------------------------------------------- #
# The tracker
# --------------------------------------------------------------------------- #
def _stage_stats():
    run = db.query("SELECT id FROM journey_runs WHERE status='ok' ORDER BY id DESC LIMIT 1", fetch="one")
    if not run:
        return {}
    rows = db.query("SELECT request_type, stage, n, median_h, p90_h FROM journey_stage_stats WHERE run_id=%s",
                    (run["id"],))
    return {(r["request_type"], r["stage"]): r for r in rows}


def _owners():
    out = {}
    for role in set(STATE_ROLE.values()):
        rows = db.query("SELECT full_name FROM employees WHERE role=%s AND status='active' ORDER BY full_name", (role,))
        out[role] = [r["full_name"] for r in rows]
    return out


def tracker(rid, now=None, stats=None, owners=None):
    """Everything the tracker page shows for one request."""
    now = now or _now()
    r = get_joined(rid)
    if not r:
        return None
    svc = db.query("SELECT resource_type FROM services WHERE id=%s", (r["service_id"],), fetch="one") \
        if r["service_id"] else None
    events = db.query("""SELECT e.action, e.note, e.created_at, em.full_name AS actor_name
                         FROM request_events e LEFT JOIN employees em ON em.id = e.actor_id
                         WHERE e.request_id=%s ORDER BY e.created_at, e.id""", (rid,))
    b = live_booking(rid, include_done=True)
    booking = None
    if b:
        booking = {"starts_at": b["starts_at"], "ends_at": b["ends_at"], "location": b["location"],
                   "method": b["method"], "reason": b["reason"]}
    elif r["appointment_at"]:     # booked by hand before CHANGE 011, or through the old API field
        booking = {"starts_at": r["appointment_at"], "ends_at": r["appointment_at"] + timedelta(minutes=SLOT_MINUTES),
                   "location": LOCATION, "method": "manual", "reason": None}

    upcoming = None

    def projected(earliest):
        nonlocal upcoming
        if upcoming is None:
            upcoming = free_slots(_now(), exclude_request=rid)
        slot, _ = tl.choose_slot(upcoming, earliest, other_visits(r["target_employee_id"], rid))
        return slot

    req = {
        "id": r["id"], "type": r["type"], "status": r["status"], "created_at": r["created_at"],
        "target_name": r["target_name"], "requestor_name": r["requestor_name"],
        "requestor_is_system": is_system_actor_id(r["requestor_id"]),
        "resource_type": svc and svc["resource_type"], "reject_reason": r["reject_reason"],
        "summary": _summary(r),
    }
    t = tl.build_tracker(req, events, now=now, booking=booking,
                         stats=_stage_stats() if stats is None else stats,
                         owners=_owners() if owners is None else owners,
                         projected_slot=projected, lead_hours=LEAD_HOURS, auto_match=AUTO_MATCH)
    t["target_employee_id"] = r["target_employee_id"]
    t["requestor_id"] = r["requestor_id"]
    return t


def _summary(r):
    if r["type"] == "onboard":
        return f"Joining {r['new_department_name'] or 'a department'}"
    if r["type"] == "shift":
        return f"Moving from {r['source_department_name'] or '?'} to {r['new_department_name'] or '?'}"
    if r["type"] == "return":
        return f"Returning {r['service_name'] or 'equipment'}"
    return f"New access: {r['service_name'] or 'a service'}"
