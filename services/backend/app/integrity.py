"""Mandatory Block Leave (leave clearance) and casino visit declarations  (CHANGE 012).

Sits next to the Mandatory Job Rotation tracker (CHANGE 006) on the Director / HR dashboard.

Mandatory Block Leave (MBL) — clearing remaining annual leave by 31 December
  Each officer's leave for the year = entitlement for the year + leave carried forward from
  last year. Leave taken or booked is recorded by HR. Whatever is left must be cleared by the
  end of the calendar year. MOM: unused annual leave that is carried forward into the next
  12 months can be forfeited if it is not used by the end of that period (MOM, "Annual leave
  in special situations"), so leave carried in from last year is shown separately: it is the
  leave lost first if it is not cleared. Working days = Mon-Fri less gazetted public holidays.

Casino visits — PSD "Measures to strengthen integrity in the Public Service" (30 Sep 2013,
  in force 1 Oct 2013):
  * ALL officers declare within 7 days when they visit the local casinos MORE THAN 4 TIMES
    in a calendar month, or buy an ANNUAL entry-levy pass;
  * officers in SENSITIVE POSITIONS declare EVERY visit within 7 days.
  Whether an officer is in a sensitive position (employees.sensitive_post) is set by IT Admin.
  PSD reply to PQ (22 Nov 2010): no gambling during working hours or on official premises.
"""
import os
from datetime import date, datetime, timedelta, timezone

from . import audit, db
from .mailer import send_email

SGT = timezone(timedelta(hours=8))


def _int_env(name, default):
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


DEFAULT_ENTITLEMENT = _int_env("MBL_DEFAULT_ENTITLEMENT_DAYS", 14)   # when HR has not set one for the year
AT_RISK_FROM = (10, 1)           # (month, day): leave still not booked from then on -> at risk
DECLARE_WITHIN_DAYS = 7
GENERAL_MONTHLY_LIMIT = 4        # visits a month a non-sensitive officer may make without declaring
CASINOS = ("Marina Bay Sands", "Resorts World Sentosa")
ENTRY_LEVY = {"daily": 150, "annual": 3000}   # S$, Singapore Citizens and PRs

SYSTEM = {"id": None, "username": "system:integrity", "role": "system"}


class IntegrityError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.message, self.status = message, status


def _today():
    return datetime.now(SGT).date()


def _as_date(v, field):
    if isinstance(v, date):
        return v
    try:
        return date.fromisoformat(str(v or "").strip())
    except ValueError:
        raise IntegrityError(f"{field} must be a date (YYYY-MM-DD)")


def _as_days(v, field):
    try:
        f = float(v)
    except (TypeError, ValueError):
        raise IntegrityError(f"{field} must be a number of days")
    if f < 0 or f > 60 or f * 2 != int(f * 2):
        raise IntegrityError(f"{field} must be between 0 and 60, in half days")
    return f


# --------------------------------------------------------------------------- #
# Working days
# --------------------------------------------------------------------------- #
def holidays():
    return {r["holiday"]: r["name"] for r in db.query("SELECT holiday, name FROM public_holidays")}


def holiday_years():
    return sorted({d.year for d in holidays()})


def working_days(start, end, hols=None):
    """Mon-Fri between start and end inclusive, less gazetted public holidays."""
    hols = holidays() if hols is None else hols
    n, d = 0, start
    while d <= end:
        if d.weekday() < 5 and d not in hols:
            n += 1
        d += timedelta(days=1)
    return n


def latest_start(days, year, today, hols):
    """Last day an officer can start leave and still clear `days` working days by 31 Dec
    (taking them back to back). None if it can no longer be done."""
    if days <= 0:
        return None
    need, d, n = int(-(-days // 1)), date(year, 12, 31), 0     # round half days up
    while d >= today:
        if d.weekday() < 5 and d not in hols:
            n += 1
            if n == need:
                return d
        d -= timedelta(days=1)
    return None


# --------------------------------------------------------------------------- #
# Officers in scope: ordinary employees in a department, not inactive
# --------------------------------------------------------------------------- #
_OFFICERS = """
    SELECT e.id, e.username, e.full_name, e.email, e.status, e.sensitive_post,
           e.department_id, d.name AS department_name
    FROM employees e LEFT JOIN departments d ON d.id = e.department_id
    WHERE e.role = 'employee' AND e.department_id IS NOT NULL AND e.status <> 'inactive'
"""


def _officer_rows(emp_id=None):
    if emp_id is not None:
        return db.query(_OFFICERS + " AND e.id = %s", (emp_id,))
    return db.query(_OFFICERS + " ORDER BY d.name, e.full_name")


def officers():
    """Everyone in scope with their sensitive-position flag (IT Admin's list)."""
    return [{"employee_id": o["id"], "username": o["username"], "full_name": o["full_name"],
             "department_name": o["department_name"], "sensitive_post": o["sensitive_post"]}
            for o in _officer_rows()]


def set_sensitive(emp_id, sensitive, reason, actor):
    rows = _officer_rows(emp_id)
    if not rows:
        raise IntegrityError("Officer not found", 404)
    if not isinstance(sensitive, bool):
        raise IntegrityError("sensitive must be true or false")
    o = rows[0]
    if o["sensitive_post"] != sensitive:
        db.query("UPDATE employees SET sensitive_post=%s WHERE id=%s", (sensitive, emp_id), fetch=None)
        audit.record("workflow", "sensitive_post_changed", target_type="employee", target_id=emp_id,
                     target_label=o["username"],
                     details={"from": o["sensitive_post"], "to": sensitive,
                              "reason": (reason or "").strip() or None})
    return [x for x in officers() if x["employee_id"] == emp_id][0]


# --------------------------------------------------------------------------- #
# Mandatory Block Leave: remaining leave to clear by 31 December
# --------------------------------------------------------------------------- #
def _record_dict(r, year, hols, today):
    s, e = max(r["start_date"], date(year, 1, 1)), min(r["end_date"], date(year, 12, 31))
    taken = working_days(s, min(e, today - timedelta(days=1)), hols) if s < today else 0
    booked = working_days(max(s, today), e, hols) if e >= today else 0
    return {"id": r["id"], "employee_id": r["employee_id"],
            "start_date": r["start_date"].isoformat(), "end_date": r["end_date"].isoformat(),
            "working_days": taken + booked, "taken": taken, "booked": booked, "note": r["note"],
            "timing": "taken" if r["end_date"] < today else "on_leave" if r["start_date"] <= today else "booked",
            "recorded_by_name": r.get("recorded_by_name")}


def _records(year, emp_id=None):
    sql = """SELECT lr.*, r.full_name AS recorded_by_name FROM leave_records lr
             LEFT JOIN employees r ON r.id = lr.recorded_by
             WHERE lr.status = 'approved' AND lr.start_date <= %s AND lr.end_date >= %s"""
    params = [date(year, 12, 31), date(year, 1, 1)]
    if emp_id is not None:
        sql += " AND lr.employee_id = %s"
        params.append(emp_id)
    return db.query(sql + " ORDER BY lr.start_date", tuple(params))


def _entitlements(year):
    return {r["employee_id"]: r for r in
            db.query("SELECT * FROM leave_entitlements WHERE year=%s", (year,))}


def mbl_state(remaining, booked, year, today, hols):
    """cleared / booked / to_clear / at_risk / cannot_clear / forfeited."""
    if remaining <= 0:
        return "booked" if booked > 0 else "cleared"
    if year < today.year:
        return "forfeited"
    if year == today.year and remaining > working_days(today, date(year, 12, 31), hols):
        return "cannot_clear"
    if year == today.year and (today.month, today.day) >= AT_RISK_FROM:
        return "at_risk"
    return "to_clear"


def _num(x):
    return int(x) if float(x).is_integer() else float(x)


def mbl_officers(year=None, today=None):
    today = today or _today()
    year = year or today.year
    hols = holidays()
    ents = _entitlements(year)
    recs = {}
    for r in _records(year):
        recs.setdefault(r["employee_id"], []).append(_record_dict(r, year, hols, today))
    left_in_year = working_days(max(today, date(year, 1, 1)), date(year, 12, 31), hols) if year >= today.year else 0
    out = []
    for o in _officer_rows():
        ent = ents.get(o["id"])
        entitled = float(ent["entitled_days"]) if ent else float(DEFAULT_ENTITLEMENT)
        carried = float(ent["carried_forward_days"]) if ent else 0.0
        rs = recs.get(o["id"], [])
        taken = sum(r["taken"] for r in rs)
        booked = sum(r["booked"] for r in rs)
        remaining = entitled + carried - taken - booked
        state = mbl_state(remaining, booked, year, today, hols)
        start_by = latest_start(remaining, year, today, hols) if year == today.year else None
        out.append({"employee_id": o["id"], "username": o["username"], "full_name": o["full_name"],
                    "department_name": o["department_name"], "status": o["status"],
                    "entitled": _num(entitled), "carried_forward": _num(carried), "entitlement_set": bool(ent),
                    "taken": taken, "booked": booked, "remaining": _num(max(remaining, 0)),
                    "carried_forward_left": _num(max(min(carried, remaining), 0)),
                    "state": state, "records": rs,
                    "start_by": start_by.isoformat() if start_by else None})
    order = {"cannot_clear": 0, "forfeited": 0, "at_risk": 1, "to_clear": 2, "booked": 3, "cleared": 4}
    out.sort(key=lambda r: (order.get(r["state"], 9), -r["remaining"], r["full_name"]))
    return {"year": year, "working_days_left": left_in_year, "officers": out}


def set_entitlement(emp_id, year, entitled, carried, actor):
    if not _officer_rows(emp_id):
        raise IntegrityError("Officer not found", 404)
    try:
        year = int(year)
    except (TypeError, ValueError):
        raise IntegrityError("year must be a number")
    entitled, carried = _as_days(entitled, "Entitlement"), _as_days(carried or 0, "Carried forward")
    db.query("""INSERT INTO leave_entitlements (employee_id, year, entitled_days, carried_forward_days, updated_by)
                VALUES (%s,%s,%s,%s,%s)
                ON CONFLICT (employee_id, year) DO UPDATE SET entitled_days=EXCLUDED.entitled_days,
                    carried_forward_days=EXCLUDED.carried_forward_days, updated_by=EXCLUDED.updated_by,
                    updated_at=now()""", (emp_id, year, entitled, carried, actor["id"]), fetch=None)
    audit.record("workflow", "leave_entitlement_set", target_type="employee", target_id=emp_id,
                 details={"year": year, "entitled_days": entitled, "carried_forward_days": carried})
    return [o for o in mbl_officers(year)["officers"] if o["employee_id"] == emp_id][0]


def record_leave(emp_id, start, end, note, actor):
    start, end = _as_date(start, "Start date"), _as_date(end, "End date")
    if end < start:
        raise IntegrityError("The leave cannot end before it starts")
    if (end - start).days > 60:
        raise IntegrityError("One leave record is at most 60 calendar days")
    if start.year != end.year:
        raise IntegrityError("Record leave that crosses 31 Dec as two records, one per year")
    rows = _officer_rows(emp_id)
    if not rows:
        raise IntegrityError("Officer not found", 404)
    clash = db.query("""SELECT id FROM leave_records WHERE employee_id=%s AND status='approved'
                        AND start_date <= %s AND end_date >= %s""", (emp_id, end, start), fetch="one")
    if clash:
        raise IntegrityError("This overlaps leave already recorded for the officer", 409)
    hols = holidays()
    if working_days(start, end, hols) == 0:
        raise IntegrityError("Those dates contain no working days")
    row = db.query("""INSERT INTO leave_records (employee_id, start_date, end_date, note, recorded_by)
                      VALUES (%s,%s,%s,%s,%s) RETURNING *""",
                   (emp_id, start, end, (note or "").strip() or None, actor["id"]), fetch="one")
    rec = _record_dict(row, start.year, hols, _today())
    audit.record("workflow", "leave_recorded", target_type="employee", target_id=emp_id,
                 target_label=rows[0]["username"],
                 details={"start": rec["start_date"], "end": rec["end_date"],
                          "working_days": rec["working_days"], "record_id": row["id"]})
    summary = [o for o in mbl_officers(start.year)["officers"] if o["employee_id"] == emp_id][0]
    rec["remaining"] = summary["remaining"]
    return rec


def cancel_leave(record_id, actor):
    row = db.query("""UPDATE leave_records SET status='cancelled' WHERE id=%s AND status='approved'
                      RETURNING employee_id, start_date, end_date""", (record_id,), fetch="one")
    if not row:
        raise IntegrityError("Leave record not found (or already cancelled)", 404)
    audit.record("workflow", "leave_cancelled", target_type="employee", target_id=row["employee_id"],
                 details={"record_id": record_id, "start": row["start_date"].isoformat(),
                          "end": row["end_date"].isoformat()})
    return {"id": record_id, "status": "cancelled"}


def remind_mbl(actor=None):
    """E-mail every officer who still has leave to clear this year (copy to HR)."""
    today = _today()
    data = mbl_officers(today.year, today)
    pending = [o for o in data["officers"] if o["remaining"] > 0]
    if not pending:
        return 0
    emails = {r["id"]: r["email"] for r in db.query("SELECT id, email FROM employees WHERE id = ANY(%s)",
                                                    ([o["employee_id"] for o in pending],))}
    for o in pending:
        if emails.get(o["employee_id"]):
            cf = (f" {o['carried_forward_left']} of these days were carried forward from last year and "
                  "are forfeited if not used." if o["carried_forward_left"] else "")
            send_email(emails[o["employee_id"]],
                       f"[ServiceJourney] Mandatory Block Leave — {o['remaining']} day(s) to clear by 31 Dec {today.year}",
                       f"Dear {o['full_name']},\n\nYou have {o['remaining']} day(s) of annual leave still to clear "
                       f"by 31 December {today.year}.{cf}\nPlease apply for it"
                       + (f" and start no later than {o['start_by']}." if o["start_by"] else ".")
                       + "\n\nThis message was generated automatically by ServiceJourney.")
    lines = "\n".join(f"  - {o['full_name']} ({o['username']}), {o['department_name']}: {o['remaining']} day(s) left"
                      + (f", start by {o['start_by']}" if o["start_by"] else ", cannot be cleared in time")
                      for o in pending)
    for p in db.query("SELECT email FROM employees WHERE role='hr' AND status='active' AND email IS NOT NULL"):
        send_email(p["email"], f"[ServiceJourney] MBL {today.year} — {len(pending)} officer(s) reminded",
                   f"Officers with annual leave still to clear by 31 December:\n\n{lines}")
    audit.record("workflow", "mbl_reminders_sent", actor=actor or SYSTEM,
                 details={"year": today.year, "officers": [o["username"] for o in pending]})
    return len(pending)


# --------------------------------------------------------------------------- #
# Casino visits
# --------------------------------------------------------------------------- #
def rule_for(sensitive):
    if sensitive:
        return {"code": "every_visit", "text": "Declare EVERY visit to a local casino within 7 days."}
    return {"code": "frequent", "text": f"Declare within 7 days when visiting the local casinos more than "
                                        f"{GENERAL_MONTHLY_LIMIT} times in a calendar month, or on buying an annual levy pass."}


def _evaluate(rows, sensitive):
    """Flag each declaration of ONE officer (rows sorted by visit_date, id)."""
    month_count, out = {}, []
    for r in rows:
        personal = r["kind"] == "visit" and not r["official_duty"]
        n = None
        if personal:
            key = (r["visit_date"].year, r["visit_date"].month)
            month_count[key] = n = month_count.get(key, 0) + 1
        if r["kind"] == "annual_levy" or sensitive:
            required = True
        else:
            required = personal and n > GENERAL_MONTHLY_LIMIT
        deadline = r["visit_date"] + timedelta(days=DECLARE_WITHIN_DAYS)
        declared_on = r["declared_at"].astimezone(SGT).date()
        flags = []
        if required and declared_on > deadline:
            flags.append("late")
        if personal and n > GENERAL_MONTHLY_LIMIT:
            flags.append("frequent")
        out.append({"id": r["id"], "employee_id": r["employee_id"], "kind": r["kind"],
                    "visit_date": r["visit_date"].isoformat(), "casino": r["casino"],
                    "official_duty": r["official_duty"], "purpose": r["purpose"],
                    "declared_at": r["declared_at"].isoformat(), "declared_on": declared_on.isoformat(),
                    "deadline": deadline.isoformat(), "required": required,
                    "visit_no_in_month": n, "flags": flags,
                    "declared_by_name": r.get("declared_by_name")})
    return out


def _decl_rows(emp_id=None, since=None):
    sql = """SELECT c.*, b.full_name AS declared_by_name FROM casino_declarations c
             LEFT JOIN employees b ON b.id = c.declared_by WHERE TRUE"""
    params = []
    if emp_id is not None:
        sql += " AND c.employee_id = %s"
        params.append(emp_id)
    if since is not None:
        sql += " AND c.visit_date >= %s"
        params.append(since)
    return db.query(sql + " ORDER BY c.employee_id, c.visit_date, c.id", tuple(params))


def _summary(o, decls, today):
    month = (today.year, today.month)
    personal_month = sum(1 for d in decls if d["kind"] == "visit" and not d["official_duty"]
                         and date.fromisoformat(d["visit_date"]).timetuple()[:2] == month)
    year_ago = today - timedelta(days=365)
    levy = [d for d in decls if d["kind"] == "annual_levy" and date.fromisoformat(d["visit_date"]) > year_ago]
    recent = [d for d in decls if date.fromisoformat(d["visit_date"]) > year_ago]
    if any("late" in d["flags"] for d in recent):
        state = "late"
    elif levy or any("frequent" in d["flags"] for d in recent
                     if date.fromisoformat(d["visit_date"]) > today - timedelta(days=90)):
        state = "frequent"
    else:
        state = "ok"
    return {"employee_id": o["id"], "username": o["username"], "full_name": o["full_name"],
            "department_name": o["department_name"], "sensitive_post": o["sensitive_post"],
            "rule": rule_for(o["sensitive_post"]), "state": state,
            "visits_this_month": personal_month,
            "annual_levy_until": (max(date.fromisoformat(d["visit_date"]) for d in levy)
                                  + timedelta(days=365)).isoformat() if levy else None,
            "declarations_12m": len(recent),
            "last_declaration": recent[-1] if recent else None}


def casino_officers(today=None):
    today = today or _today()
    by_emp = {}
    for r in _decl_rows():
        by_emp.setdefault(r["employee_id"], []).append(r)
    out = [_summary(o, _evaluate(by_emp.get(o["id"], []), o["sensitive_post"]), today) for o in _officer_rows()]
    order = {"late": 0, "frequent": 1, "ok": 2}
    out.sort(key=lambda r: (order[r["state"]], r["department_name"] or "", r["full_name"]))
    return out


def recent_declarations(days=90, today=None):
    today = today or _today()
    offs = {o["id"]: o for o in _officer_rows()}
    by_emp, out = {}, []
    for r in _decl_rows(since=today - timedelta(days=days + 40)):   # earlier rows count toward the month
        by_emp.setdefault(r["employee_id"], []).append(r)
    for emp_id, rs in by_emp.items():
        o = offs.get(emp_id)
        if not o:
            continue
        for d in _evaluate(rs, o["sensitive_post"]):
            if date.fromisoformat(d["visit_date"]) >= today - timedelta(days=days):
                d.update(full_name=o["full_name"], username=o["username"], department_name=o["department_name"])
                out.append(d)
    out.sort(key=lambda d: (d["declared_at"], d["id"]), reverse=True)
    return out


def officer_summary(emp_id, today=None):
    today = today or _today()
    rows = _officer_rows(emp_id)
    if not rows:
        return None
    o = rows[0]
    decls = _evaluate(_decl_rows(emp_id), o["sensitive_post"])
    s = _summary(o, decls, today)
    s["declarations"] = list(reversed(decls))
    mine = [m for m in mbl_officers(today.year, today)["officers"] if m["employee_id"] == emp_id]
    s["mbl"] = mine[0] if mine else None
    return s


def declare(emp_id, kind, visit_date, casino, official_duty, purpose, actor):
    if kind not in ("visit", "annual_levy"):
        raise IntegrityError("kind must be 'visit' or 'annual_levy'")
    if casino not in CASINOS:
        raise IntegrityError("Choose the casino: " + " or ".join(CASINOS))
    vd = _as_date(visit_date, "Visit date")
    today = _today()
    if vd > today:
        raise IntegrityError("A declaration is for a visit (or levy purchase) that has already happened")
    if vd < today - timedelta(days=366):
        raise IntegrityError("That date is more than a year ago")
    official = bool(official_duty) and kind == "visit"
    purpose = (purpose or "").strip() or None
    if official and not purpose:
        raise IntegrityError("Give the official purpose of the visit")
    rows = _officer_rows(emp_id)
    if not rows:
        raise IntegrityError("Officer not found", 404)
    o = rows[0]
    row = db.query("""INSERT INTO casino_declarations (employee_id, kind, visit_date, casino, official_duty,
                                                       purpose, declared_by)
                      VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
                   (emp_id, kind, vd, casino, official, purpose, actor["id"]), fetch="one")
    d = {x["id"]: x for x in _evaluate(_decl_rows(emp_id), o["sensitive_post"])}[row["id"]]
    audit.record("workflow", "casino_declared", target_type="employee", target_id=emp_id,
                 target_label=o["username"],
                 details={"declaration_id": row["id"], "kind": kind, "date": d["visit_date"], "casino": casino,
                          "official_duty": official, "flags": d["flags"], "sensitive_post": o["sensitive_post"],
                          "on_behalf": actor["id"] != emp_id})
    if "late" in d["flags"]:
        _alert(o, d)
    return d


def _alert(o, d):
    body = (f"Officer: {o['full_name']} ({o['username']}), {o['department_name']}"
            f"{' — sensitive position' if o['sensitive_post'] else ''}\n"
            f"{'Annual levy bought' if d['kind'] == 'annual_levy' else 'Visit'}: {d['visit_date']} at {d['casino']}\n"
            f"Issue: LATE DECLARATION (due {d['deadline']}, declared {d['declared_on']})\n\n"
            "Follow up under the agency's disciplinary procedure; see the console > Casino visits.")
    subject = f"[ServiceJourney] Late casino declaration — {o['full_name']}"
    for p in db.query("SELECT email FROM employees WHERE role IN ('hr','director') AND status='active' "
                      "AND email IS NOT NULL ORDER BY id"):
        send_email(p["email"], subject, body)


def settings():
    return {"declare_within_days": DECLARE_WITHIN_DAYS, "general_monthly_limit": GENERAL_MONTHLY_LIMIT,
            "casinos": list(CASINOS), "entry_levy": ENTRY_LEVY, "holiday_years": holiday_years(),
            "default_entitlement": DEFAULT_ENTITLEMENT,
            "at_risk_from": f"{AT_RISK_FROM[1]} {date(2000, AT_RISK_FROM[0], 1):%b}"}
