"""Offline checks for the journey tracker and smart appointment matching (CHANGE 011).

    cd services/backend && python test_tracker.py

No database, Keycloak or network needed: tracker_logic.py is pure. (app/__init__.py is
bypassed by loading the file directly, so the backend's required secrets are not needed.)
"""
import importlib.util
import pathlib
from datetime import date, datetime, timedelta, timezone

spec = importlib.util.spec_from_file_location("tracker_logic", pathlib.Path(__file__).parent / "app" / "tracker_logic.py")
tl = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tl)

SGT = tl.SGT
failed = []


def check(name, ok):
    print(("PASS  " if ok else "FAIL  ") + name)
    if not ok:
        failed.append(name)


def at(d, hh, mm=0):
    return datetime(2026, 11, d, hh, mm, tzinfo=SGT).astimezone(timezone.utc)


# ---------------------------------------------------------------- the roster
hours = tl.parse_hours("09:00-12:00,14:00-17:00")
slots = tl.roster_slots(date(2026, 11, 13), 7, hours, tl.parse_days("0,1,2,3,4"), 30)   # Fri 13 Nov ..
days = {s.astimezone(SGT).date() for s, _ in slots}
check("roster: 12 half-hour slots per working day", len(slots) == 12 * len(days))
check("roster: no weekend slots", all(d.weekday() < 5 for d in days) and date(2026, 11, 14) not in days)
check("roster: first slot is 09:00 SGT", slots[0][0].astimezone(SGT).strftime("%H:%M") == "09:00")
check("roster: no slot over lunch", not any(s.astimezone(SGT).hour == 12 or s.astimezone(SGT).hour == 13 for s, _ in slots))
check("roster: last slot ends 17:00", max(e for _, e in slots).astimezone(SGT).strftime("%H:%M") == "17:00")
check("roster: a slot never runs past its block",
      len(tl.roster_slots(date(2026, 11, 16), 1, tl.parse_hours("09:00-10:45"), {0}, 30)) == 3)
try:
    tl.parse_hours("12:00-09:00")
    check("roster: backwards hours refused", False)
except ValueError:
    check("roster: backwards hours refused", True)

# ---------------------------------------------------------------- the smart match
S = [{"id": i, "starts_at": at(17, 9) + timedelta(minutes=30 * i),
      "ends_at": at(17, 9) + timedelta(minutes=30 * (i + 1)), "free": 2} for i in range(6)]
slot, why = tl.choose_slot(S, at(17, 9))
check("match: earliest free slot", slot["id"] == 0 and "earliest" in why)
slot, _ = tl.choose_slot(S, at(17, 10))
check("match: respects the preparation lead time", slot["id"] == 2)
full = [dict(s, free=0) if s["id"] < 3 else s for s in S]
check("match: skips full slots", tl.choose_slot(full, at(17, 9))[0]["id"] == 3)
check("match: none free -> None", tl.choose_slot([dict(s, free=0) for s in S], at(17, 9))[0] is None)
visit = [(at(17, 10, 30), at(17, 11))]         # the employee already comes at 10:30
slot, why = tl.choose_slot(S, at(17, 9), visit)
check("match: bundles with the employee's other appointment", slot["id"] in (2, 4) and "one visit" in why)
check("match: picks the slot right before it when free", slot["id"] == 2)
check("match: never double-books the employee",
      all(tl.choose_slot([s], at(17, 9), visit)[0] is None for s in S if s["id"] == 3))
shuffled = list(reversed(S))
check("match: input order does not matter", tl.choose_slot(shuffled, at(17, 9))[0]["id"] == 0)

# ---------------------------------------------------------------- what to bring
check("bring: onboarding asks for the acceptable-use form",
      any("Acceptable Use" in i for i in tl.what_to_bring("onboard")))
check("bring: return of a laptop asks for the laptop and a backup",
      any("charger" in i for i in tl.what_to_bring("return", "laptop"))
      and any("Back up" in i for i in tl.what_to_bring("return", "laptop")))
check("bring: shift asks for the current laptop", any("current laptop" in i for i in tl.what_to_bring("shift")))
check("bring: always ID and the request number",
      all(any("staff pass" in i for i in tl.what_to_bring(t, None, 7)) and "Your request number: #7" in tl.what_to_bring(t, None, 7)
          for t in ("onboard", "grant", "shift", "return")))

# ---------------------------------------------------------------- the tracker
NOW = at(14, 10)
OWNERS = {"hr": ["Harper Lim"], "director": ["Dana Tan"], "aa": ["Avery Ng"], "it_admin": ["Ian Koh"],
          "euss": ["Eve Lee"]}
base = {"id": 41, "type": "onboard", "created_at": at(13, 9), "target_name": "Nina New",
        "requestor_name": "Rita Requestor", "requestor_is_system": False, "resource_type": None,
        "reject_reason": None}


def ev(action, ts, who="x", note=None):
    return {"action": action, "created_at": ts, "actor_name": who, "note": note}


def proj(earliest):
    return {"starts_at": earliest + timedelta(hours=1), "ends_at": earliest + timedelta(hours=1, minutes=30),
            "location": "EUSS desk"}


t = tl.build_tracker(dict(base, status="pending_director"),
                     [ev("submitted", at(13, 9), "Rita"), ev("approved", at(13, 15), "Harper Lim")],
                     now=NOW, owners=OWNERS, projected_slot=proj)
steps = {s["key"]: s for s in t["steps"]}
check("tracker: HR step done, by whom and when", steps["pending_hr"]["state"] == "done"
      and steps["pending_hr"]["by"] == "Harper Lim" and steps["pending_hr"]["at"] == at(13, 15).isoformat())
check("tracker: current step names who it waits on", steps["pending_director"]["state"] == "current"
      and "Dana Tan" in steps["pending_director"]["waiting_on"] and t["headline"].startswith("Waiting on the Director"))
check("tracker: later steps upcoming with rising estimates",
      all(steps[k]["state"] == "upcoming" for k in ("pending_aa", "pending_it", "pending_euss"))
      and steps["pending_aa"]["eta"] < steps["pending_it"]["eta"] < steps["pending_euss"]["eta"])
check("tracker: appointment step is automatic", "automatic" in steps["pending_appointment"]["waiting_on"])
check("tracker: estimated finish uses the projected EUSS slot",
      t["eta"] == steps["pending_euss"]["eta"] and "Likely appointment" in (steps["pending_euss"]["note"] or ""))
check("tracker: overall finish is the last step's", t["eta"] == steps["completed"]["eta"])
check("tracker: onboarding wording", steps["pending_it"]["label"].startswith("IT sets up your accounts"))
check("tracker: default estimates are labelled as such", "standard turnaround" in t["estimate_basis"])
check("tracker: what to bring included", any("Acceptable Use" in i for i in t["what_to_bring"]))

late = tl.build_tracker(dict(base, status="pending_director"),
                        [ev("submitted", at(10, 9)), ev("approved", at(10, 10))],
                        now=NOW, owners=OWNERS, projected_slot=proj)
check("tracker: flags a step that is taking longer than usual",
      late["late"] and {s["key"]: s for s in late["steps"]}["pending_director"]["late"])

stats = {("onboard", "pending_director"): {"n": 12, "median_h": 30.0, "p90_h": 60.0},
         ("all", "pending_aa"): {"n": 40, "median_h": 2.0, "p90_h": 5.0},
         ("onboard", "pending_aa"): {"n": 1, "median_h": 99.0, "p90_h": 99.0}}
t2 = tl.build_tracker(dict(base, status="pending_director"),
                      [ev("submitted", at(13, 9)), ev("approved", at(13, 15))],
                      now=NOW, owners=OWNERS, stats=stats, projected_slot=proj)
s2 = {s["key"]: s for s in t2["steps"]}
check("tracker: uses the analytics median for the request type",
      s2["pending_director"]["basis"] == "type" and s2["pending_director"]["eta"] == tl.office_time(at(13, 15) + timedelta(hours=30)).isoformat())
check("office hours: a Saturday estimate moves to Monday 09:00",
      tl.office_time(at(14, 6, 57)) == at(16, 9) and tl.office_time(at(16, 20)) == at(17, 9)
      and tl.office_time(at(17, 7)) == at(17, 9) and tl.office_time(at(17, 11)) == at(17, 11))
check("tracker: no approval step is expected at night or at the weekend",
      all(datetime.fromisoformat(x["eta"]).astimezone(SGT).weekday() < 5 and 9 <= datetime.fromisoformat(x["eta"]).astimezone(SGT).hour < 18
          for x in t["steps"] if x["eta"] and x["key"] in ("pending_hr", "pending_director", "pending_aa", "pending_it")))
check("tracker: falls back to all types when the type has too little history",
      s2["pending_aa"]["basis"] == "all" and s2["pending_aa"]["history_n"] == 40)
check("tracker: basis text mentions analytics", "analytics" in t2["estimate_basis"])

booking = {"starts_at": at(17, 10), "ends_at": at(17, 10, 30), "location": "EUSS Service Desk, Level 1",
           "method": "auto", "reason": "earliest free EUSS slot"}
evs = [ev("submitted", at(13, 9)), ev("approved", at(13, 10)), ev("approved", at(13, 11)), ev("approved", at(13, 12)),
       ev("processed", at(13, 14), "Ian Koh"), ev("appointment_booked", at(13, 14), "Appointment Scheduler (Automated)")]
t3 = tl.build_tracker(dict(base, status="pending_euss"), evs, now=NOW, owners=OWNERS, booking=booking)
s3 = {s["key"]: s for s in t3["steps"]}
check("tracker: booked -> headline is the appointment", t3["headline"] == "Appointment booked"
      and "Tue 17 Nov, 10:00" in t3["subline"] and "Level 1" in t3["subline"])
check("tracker: auto-matched step says so", s3["pending_appointment"]["state"] == "done"
      and "automatically" in (s3["pending_appointment"]["note"] or ""))
check("tracker: finish estimate is the appointment end", t3["eta"] == at(17, 10, 30).isoformat())
check("tracker: appointment block exposed", t3["appointment"]["location"].startswith("EUSS") and t3["appointment"]["method"] == "auto")

missed = tl.build_tracker(dict(base, status="pending_euss"), evs, now=at(18, 9), owners=OWNERS, booking=booking)
check("tracker: appointment time passed -> flagged", missed["late"])

done = tl.build_tracker(dict(base, status="completed"), evs + [ev("provisioned", at(17, 10, 20), "Eve Lee")],
                        now=at(18, 9), owners=OWNERS, booking=booking)
check("tracker: completed -> every step done, no estimate",
      done["headline"] == "Done" and done["eta"] is None and all(s["state"] == "done" for s in done["steps"])
      and done["progress_pct"] == 100)

rej = tl.build_tracker(dict(base, status="rejected", reject_reason="Wrong department"),
                       [ev("submitted", at(13, 9)), ev("approved", at(13, 10), "Harper Lim"),
                        ev("rejected", at(13, 11), "Dana Tan", "Wrong department")],
                       now=NOW, owners=OWNERS)
r = {s["key"]: s for s in rej["steps"]}
check("tracker: rejection shows where and why", r["pending_director"]["state"] == "rejected"
      and r["pending_director"]["note"] == "Wrong department" and r["pending_hr"]["state"] == "done"
      and r["pending_aa"]["state"] == "skipped" and rej["headline"] == "Stopped" and rej["eta"] is None)

sysreq = tl.build_tracker(dict(base, status="pending_appointment", requestor_is_system=True),
                          evs[:5], now=NOW, owners=OWNERS, auto_match=False)
check("tracker: manual booking step waits on the Requestor team for HR-system requests",
      sysreq["headline"] == "Waiting on the Requestor team")

ret = tl.build_tracker(dict(base, type="return", status="pending_hr", resource_type="laptop"),
                       [ev("submitted", at(14, 9))], now=NOW, owners=OWNERS, projected_slot=proj)
check("tracker: return wording", {s["key"]: s for s in ret["steps"]}["pending_euss"]["label"].startswith("Return your items"))

print()
print(f"{len(failed)} failed" if failed else "all checks passed")
raise SystemExit(1 if failed else 0)
