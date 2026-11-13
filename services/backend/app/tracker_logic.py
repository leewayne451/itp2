"""New-hire journey tracker and smart appointment booking (CHANGE 011): the pure part.

No database, Flask or network here, so it can be tested on its own (test_tracker.py).
The database side (slots, bookings, who to ask) is in scheduling.py.

Three jobs:

  * roster_slots()  - the EUSS desk's regular opening hours as appointment slots
  * choose_slot()   - which free slot to give a request (the "smart" match)
  * build_tracker() - the parcel-tracking view of one request: each step, who it is
                      waiting on, when it should be done, and what to bring
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone

# Times shown to people and the EUSS roster are Singapore time (UTC+8, no DST),
# the same convention as the console (labels.py). The database keeps UTC.
SGT = timezone(timedelta(hours=8), "SGT")

# --------------------------------------------------------------------------- #
# The workflow, as seen by the person it is about
# --------------------------------------------------------------------------- #
# Mirrors STATE_ROLE / APPROVAL_NEXT in core.py and next_state() in the analytics.
APPROVAL_NEXT = {"pending_hr": "pending_director", "pending_director": "pending_aa", "pending_aa": "pending_it"}
STAGES = ["pending_hr", "pending_director", "pending_aa", "pending_it", "pending_appointment", "pending_euss"]
STAGE_ROLE = {"pending_hr": "hr", "pending_director": "director", "pending_aa": "aa",
              "pending_it": "it_admin", "pending_appointment": "requestor", "pending_euss": "euss"}
ROLE_TEAM = {"hr": "HR", "director": "the Director", "aa": "the Approving Authority",
             "it_admin": "IT Admin", "requestor": "the Requestor", "euss": "EUSS"}

# Used until the analytics pipeline has enough history (calendar hours per stage).
DEFAULT_STAGE_HOURS = {"pending_hr": 8.0, "pending_director": 12.0, "pending_aa": 12.0,
                       "pending_it": 8.0, "pending_appointment": 4.0, "pending_euss": 1.0}
MIN_HISTORY = 3          # past stage visits needed before the analytics figure is trusted


OFFICE_START, OFFICE_END = time(9, 0), time(18, 0)


def office_time(dt):
    """Approvers work office hours: an estimate that lands at night or on a weekend
    is moved to the start of the next working day (Singapore time)."""
    local = dt.astimezone(SGT)
    if local.weekday() < 5 and OFFICE_START <= local.time() < OFFICE_END:
        return dt
    day = local.date() if local.time() < OFFICE_START else local.date() + timedelta(days=1)
    while day.weekday() >= 5:
        day += timedelta(days=1)
    return datetime.combine(day, OFFICE_START, SGT).astimezone(dt.tzinfo or timezone.utc)


def next_state(state, action):
    """Workflow state after `action`, or None when the action does not move the request."""
    if action == "submitted":
        return "pending_hr"
    if action == "approved":
        return APPROVAL_NEXT.get(state)
    if action == "processed":
        return "pending_appointment"
    if action == "appointment_booked":
        return "pending_euss"
    if action in ("provisioned", "retrieved"):
        return "completed"
    if action == "rejected":
        return "rejected"
    return None


def step_label(stage, rtype):
    """What the step means to the employee, worded per request type."""
    it = {"onboard": "IT sets up your accounts and access",
          "grant": "IT grants the access",
          "shift": "IT moves your access to the new department",
          "return": "IT confirms the offboarding"}
    euss = {"return": "Return your items at EUSS",
            "shift": "EUSS appointment (swap or set up equipment)"}
    return {
        "submitted": "Request received",
        "pending_hr": "HR approval",
        "pending_director": "Director approval",
        "pending_aa": "Approving Authority approval",
        "pending_it": it.get(rtype, "IT processing"),
        "pending_appointment": "Appointment booked with EUSS",
        "pending_euss": euss.get(rtype, "Collect your equipment at EUSS"),
        "completed": "All done",
    }[stage]


# --------------------------------------------------------------------------- #
# What to bring
# --------------------------------------------------------------------------- #
def what_to_bring(rtype, resource_type=None, rid=None):
    """The checklist for the EUSS appointment. `resource_type` is the requested
    service's (laptop | account | other) for grant / return requests."""
    items = ["Your staff pass, or your NRIC / passport if you do not have a pass yet"]
    if rtype == "onboard":
        items += ["Your signed IT Acceptable Use Policy (attached to your welcome e-mail)",
                  "Your mobile phone, to set up sign-in on your new laptop",
                  "Your employee number from HR, if you have it"]
    elif rtype == "grant":
        if resource_type == "laptop":
            items += ["Your signed IT Acceptable Use Policy",
                      "Your mobile phone, to set up sign-in on the new laptop"]
        else:
            items += ["Your mobile phone, to finish setting up the new account"]
    elif rtype == "shift":
        items += ["Your current laptop and its charger: EUSS re-images or swaps it for your new department",
                  "Any department-specific equipment from your old team (headsets, docks, tokens)"]
    elif rtype == "return":
        if resource_type == "laptop":
            items += ["The laptop, its charger and any accessories issued with it"]
        else:
            items += ["The laptop, its charger and any accessories issued with it",
                      "Any security token or smart card linked to your account"]
        items += ["Back up any personal files first: the laptop is wiped after it is returned"]
    if rid is not None:
        items.append(f"Your request number: #{rid}")
    return items


# --------------------------------------------------------------------------- #
# The EUSS roster -> appointment slots
# --------------------------------------------------------------------------- #
def parse_hours(spec):
    """'09:00-12:00,14:00-17:00' -> [(time(9), time(12)), (time(14), time(17))]."""
    out = []
    for part in (spec or "").split(","):
        part = part.strip()
        if not part:
            continue
        a, b = part.split("-")
        start, end = time.fromisoformat(a.strip()), time.fromisoformat(b.strip())
        if end <= start:
            raise ValueError(f"EUSS hours '{part}': the end must be after the start")
        out.append((start, end))
    return out


def parse_days(spec):
    """'0,1,2,3,4' (Monday = 0) -> {0, 1, 2, 3, 4}."""
    days = {int(d) for d in (spec or "").split(",") if d.strip()}
    if not days or any(d < 0 or d > 6 for d in days):
        raise ValueError("EUSS days must be numbers 0 (Monday) to 6 (Sunday)")
    return days


def roster_slots(first_day: date, days: int, hours, weekdays, slot_minutes: int):
    """Every slot the roster opens from `first_day` for `days` days, as
    (starts_at, ends_at) pairs in UTC. A slot never runs past the end of its block."""
    step = timedelta(minutes=slot_minutes)
    out = []
    for i in range(days):
        d = first_day + timedelta(days=i)
        if d.weekday() not in weekdays:
            continue
        for start, end in hours:
            t = datetime.combine(d, start, SGT)
            stop = datetime.combine(d, end, SGT)
            while t + step <= stop:
                out.append((t.astimezone(timezone.utc), (t + step).astimezone(timezone.utc)))
                t += step
    return out


# --------------------------------------------------------------------------- #
# The smart match
# --------------------------------------------------------------------------- #
def _overlaps(a_start, a_end, b_start, b_end):
    return a_start < b_end and b_start < a_end


def choose_slot(slots, earliest, other_visits=()):
    """Pick the slot for a request. Returns (slot, reason) or (None, reason).

    slots          dicts with id, starts_at, ends_at, free (places left); any order
    earliest       no slot before this (EUSS needs lead time to prepare the equipment)
    other_visits   (starts_at, ends_at) of the same employee's other booked appointments

    Rules, in order:
      1. only slots with a free place, starting at or after `earliest`;
      2. never at the same time as the employee's other appointment;
      3. if the employee already has an appointment, prefer a slot right before or
         right after it, so they make one trip to EUSS instead of two;
      4. otherwise the earliest slot (ties: the one with more places left).
    """
    usable = sorted((s for s in slots if s["free"] > 0 and s["starts_at"] >= earliest),
                    key=lambda s: (s["starts_at"], -s["free"], s["id"]))
    usable = [s for s in usable
              if not any(_overlaps(s["starts_at"], s["ends_at"], a, b) for a, b in other_visits)]
    if not usable:
        return None, "no free EUSS slot"
    for a, b in sorted(other_visits):
        for s in usable:
            if s["starts_at"] == b or s["ends_at"] == a:
                return s, "next to your other EUSS appointment, so it is one visit"
    return usable[0], "earliest free EUSS slot"


# --------------------------------------------------------------------------- #
# The tracker
# --------------------------------------------------------------------------- #
def _iso(v):
    return v.isoformat() if v else None


def replay(events):
    """When each stage was entered / left, and by whom it was moved on, from the
    request's events (oldest first). Returns (state, entered, left, left_by, rejected_at):
    rejected_at is the stage that was open when the request was rejected, if it was."""
    state, entered, left, left_by, rejected_at = None, {}, {}, {}, None
    for e in events:
        new = next_state(state, e["action"])
        if new is None:
            continue
        if new == "rejected":
            rejected_at = state
        if state is not None:
            left[state] = e["created_at"]
            left_by[state] = e.get("actor_name")
        if new not in ("completed", "rejected"):
            entered[new] = e["created_at"]
        state = new
    return state, entered, left, left_by, rejected_at


def stage_hours(stats, rtype):
    """{stage: (median_h, p90_h, n, source)} from the analytics stage statistics.

    stats: {(request_type, stage): {"median_h", "p90_h", "n"}}. The request's own type is
    used when it has enough history, then all types, then the built-in default."""
    out = {}
    for stage in STAGES:
        for key, src in (((rtype, stage), "type"), (("all", stage), "all")):
            s = stats.get(key)
            if s and (s.get("n") or 0) >= MIN_HISTORY and s.get("median_h") is not None:
                out[stage] = (float(s["median_h"]), float(s["p90_h"] or s["median_h"]), int(s["n"]), src)
                break
        else:
            d = DEFAULT_STAGE_HOURS[stage]
            out[stage] = (d, d * 2, 0, "default")
    return out


def build_tracker(req, events, *, now, booking=None, stats=None, owners=None,
                  projected_slot=None, lead_hours=24.0, auto_match=True):
    """The parcel-tracking view of one request.

    req             type, status, id, created_at, target_name, requestor_name,
                    requestor_is_system, resource_type, new_department_name, reject_reason
    events          oldest first: action, created_at (aware datetime), actor_name, note
    booking         the live EUSS booking: starts_at, ends_at, location, method, reason
    stats           analytics stage statistics, see stage_hours()
    owners          {role: [names]} of the people who act on each stage
    projected_slot  callable(earliest) -> {starts_at, ends_at, location} or None: the
                    slot the request would get if it reached the appointment step now
    """
    owners = owners or {}
    rtype, status = req["type"], req["status"]
    hours = stage_hours(stats or {}, rtype)
    _, entered, left, left_by, rejected_at = replay(events)
    def who(stage):
        role = STAGE_ROLE[stage]
        if stage == "pending_appointment":
            if req.get("requestor_is_system"):
                return "the Requestor team"
            return req.get("requestor_name") or "the Requestor"
        if stage == "pending_euss":
            return "you and EUSS" if rtype != "return" else "you (bring your items) and EUSS"
        names = owners.get(role) or []
        team = ROLE_TEAM[role]
        return f"{team} ({', '.join(names[:2])}{' …' if len(names) > 2 else ''})" if names else team

    steps = [{
        "key": "submitted", "label": step_label("submitted", rtype), "state": "done",
        "at": _iso(req["created_at"]), "by": req.get("requestor_name"), "eta": None, "note": None,
        "waiting_on": None, "late": False,
    }]
    cursor = now
    late = False
    final_eta = None
    for stage in STAGES:
        median, p90, n, src = hours[stage]
        step = {"key": stage, "label": step_label(stage, rtype), "state": "upcoming", "at": None,
                "by": None, "eta": None, "note": None, "waiting_on": who(stage), "late": False,
                "basis": src, "history_n": n}
        if stage in left and not (status == "rejected" and stage == rejected_at):
            step.update(state="done", at=_iso(left[stage]), by=left_by.get(stage), waiting_on=None)
            if stage == "pending_appointment" and booking and booking.get("method") == "auto":
                step["note"] = "Matched automatically to a free EUSS slot"
        elif status == "rejected":
            if stage == rejected_at:
                step.update(state="rejected", at=_iso(left.get(stage)), by=left_by.get(stage),
                            note=req.get("reject_reason"), waiting_on=None)
            else:
                step.update(state="skipped", waiting_on=None)
        elif stage == status:
            step["state"] = "current"
            age = (now - entered.get(stage, now)).total_seconds() / 3600
            if stage == "pending_euss" and booking:
                eta = booking["ends_at"] if booking["ends_at"] > now else now + timedelta(hours=1)
                step["late"] = booking["ends_at"] < now
                step["note"] = ("The appointment time has passed. EUSS will close this once the hand-over "
                                "is recorded." if step["late"] else None)
            else:
                remaining = max(median - age, 0.5)
                eta = office_time(now + timedelta(hours=remaining))
                step["late"] = age > max(p90, median * 1.5)
                if step["late"]:
                    step["note"] = "Taking longer than usual at this step"
            late = late or step["late"]
            step["eta"] = _iso(eta)
            cursor = eta
        else:   # still ahead
            if stage == "pending_appointment" and auto_match:
                step["waiting_on"] = "automatic: the system books the first free EUSS slot"
                step["eta"] = _iso(cursor)
            elif stage == "pending_euss":
                slot = projected_slot(cursor + timedelta(hours=lead_hours)) if projected_slot else None
                if slot:
                    cursor = slot["ends_at"]
                    step["note"] = (f"Likely appointment: the first free slot after IT is done "
                                    f"({slot['starts_at'].astimezone(SGT):%a %d %b, %H:%M})")
                else:
                    cursor = cursor + timedelta(hours=lead_hours + median)
                step["eta"] = _iso(cursor)
            else:
                cursor = office_time(cursor + timedelta(hours=median))
                step["eta"] = _iso(cursor)
        steps.append(step)
        final_eta = cursor

    done = status == "completed"
    steps.append({"key": "completed", "label": step_label("completed", rtype),
                  "state": "done" if done else ("skipped" if status == "rejected" else "upcoming"),
                  "at": _iso(left.get("pending_euss")) if done else None, "by": None,
                  "eta": None if done or status == "rejected" else _iso(final_eta),
                  "note": None, "waiting_on": None, "late": False})

    # ---- the headline, like a parcel page: where it is and when it should arrive
    if done:
        headline = "Done"
        sub = f"Completed {left.get('pending_euss').astimezone(SGT):%a %d %b %Y}" if left.get("pending_euss") else "Completed"
    elif status == "rejected":
        headline = "Stopped"
        sub = f"Not approved at {step_label(rejected_at, rtype)}" if rejected_at else "Not approved"
    elif status == "pending_euss" and booking:
        headline = "Appointment booked"
        sub = (f"{booking['starts_at'].astimezone(SGT):%a %d %b, %H:%M}–{booking['ends_at'].astimezone(SGT):%H:%M}"
               f" at {booking['location']}")
    else:
        current = next(s for s in steps if s["state"] == "current")
        headline = f"Waiting on {current['waiting_on']}"
        sub = current["label"]

    done_steps = sum(1 for s in steps if s["state"] == "done")
    return {
        "request_id": req["id"], "type": rtype, "status": status,
        "target_name": req.get("target_name"),
        "summary": req.get("summary"),
        "headline": headline, "subline": sub,
        "eta": None if done or status == "rejected" else _iso(final_eta),
        "late": late,
        "progress_pct": round(100 * done_steps / len(steps)),
        "steps": steps,
        "appointment": None if not booking else {
            "starts_at": _iso(booking["starts_at"]), "ends_at": _iso(booking["ends_at"]),
            "location": booking["location"], "method": booking.get("method"),
            "reason": booking.get("reason"),
        },
        "what_to_bring": what_to_bring(rtype, req.get("resource_type"), req["id"]),
        "estimate_basis": _basis_text(hours),
    }


def _basis_text(hours):
    n = sum(h[2] for h in hours.values())
    if n == 0:
        return "Estimated from standard turnaround times (not enough past requests yet)."
    return f"Estimated from how long {n} past steps took (service journey analytics)."
