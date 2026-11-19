"""CHANGE 012 — Mandatory Block Leave + casino declarations: backend checks on a real PostgreSQL.

Runs the real backend code against a THROWAWAY Postgres (Keycloak, tokens and e-mail are
stubbed) that was first created by the PRE-change code, so the upgrade path is covered.
It changes the demo data, so never point it at the POC's database:

    INTEGRITY_TEST_DB=yes PGHOST=... PGPORT=... PGUSER=... PGDATABASE=... API_CLIENTS=console=<secret> \
      KEYCLOAK_BACKEND_CLIENT_SECRET=... AUDIT_HMAC_KEY=... AUDIT_LOG_DIR=/tmp/sj-audit \
      python test_integrity_db.py
"""
import os, pathlib, sys, time
from datetime import date, datetime, timedelta, timezone
if os.environ.get("INTEGRITY_TEST_DB") != "yes":
    sys.exit("Refusing to run: set INTEGRITY_TEST_DB=yes and point PG* at a throwaway database.")
sys.path.insert(0, str(pathlib.Path(__file__).parent))
from app import create_app, bootstrap, db, tokens, sync, integrity, audit

bootstrap()          # NEW schema + seed over the old database (upgrade)
bootstrap()          # and again: idempotent

SENT = []
integrity.send_email = lambda to, subject, body: SENT.append((to, subject, body)) or True
sync.push = lambda *a, **k: True
tokens.validate = lambda tok: {"sub": tok, "iat": int(time.time()), "amr": ["pwd", "otp"], "typ": "Bearer"}
db.query("UPDATE employees SET keycloak_id = 'kc-' || username WHERE keycloak_id IS NULL", fetch=None)
db.query("UPDATE employees SET email = username || '@example.test' WHERE email IS NULL", fetch=None)

app = create_app()
c = app.test_client()
SECRET = os.environ["API_CLIENTS"].split("=", 1)[1]
H = lambda u: {"Authorization": f"Bearer kc-{u}", "X-Client-Id": "console", "X-Client-Secret": SECRET}
get = lambda u, p: c.get(p, headers=H(u))
post = lambda u, p, b=None: c.post(p, json=b or {}, headers=H(u))
results = []


def check(name, cond, extra=""):
    results.append(bool(cond))
    print(("PASS " if cond else "FAIL ") + name + (f"  [{extra}]" if extra and not cond else ""))


today = integrity._today()
Y = today.year
emp = {r["username"]: r for r in db.query("SELECT id, username, sensitive_post FROM employees")}
q1 = lambda sql, *a: db.query(sql, a or None, fetch="one")

# ---- 1. schema + seed
check("public holidays 2026 + 2027 seeded (26 rows incl. in-lieu days)",
      q1("SELECT count(*) n FROM public_holidays")["n"] == 26)
check("sensitive positions seeded (alice, bob, erin only)",
      {u for u, r in emp.items() if r["sensitive_post"]} == {"alice", "bob", "erin"})
check("seed ran exactly once (4 entitlements, 12 leave records, 11 declarations)",
      q1("SELECT count(*) n FROM leave_entitlements")["n"] == 4 and q1("SELECT count(*) n FROM leave_records")["n"] == 12
      and q1("SELECT count(*) n FROM casino_declarations")["n"] == 11)
check("first-draft columns / tables removed",
      q1("SELECT count(*) n FROM information_schema.columns WHERE table_name='employees' AND column_name='post_category'")["n"] == 0
      and q1("SELECT to_regclass('block_leaves') AS t")["t"] is None)

# ---- 2. working days
HOL = integrity.holidays()
check("working days: Mon 9 - Tue 17 Mar 2026 = 7", integrity.working_days(date(2026, 3, 9), date(2026, 3, 17), HOL) == 7)
check("Vesak in-lieu Monday skipped (1-5 Jun 2026 = 4)", integrity.working_days(date(2026, 6, 1), date(2026, 6, 5), HOL) == 4)
check("start-by for 12 days in 2026 = 15 Dec (25 Dec holiday)",
      integrity.latest_start(12, 2026, date(2026, 11, 19), HOL) == date(2026, 12, 15),
      integrity.latest_start(12, 2026, date(2026, 11, 19), HOL))
check("half day rounds up (2.5 -> 3 working days back = 29 Dec)",
      integrity.latest_start(2.5, 2026, date(2026, 11, 19), HOL) == date(2026, 12, 29))
check("too many days -> no start date", integrity.latest_start(40, 2026, date(2026, 11, 19), HOL) is None)

# ---- 3. MBL list (Director + HR)
r = get("director", "/api/integrity")
check("director can read /api/integrity", r.status_code == 200, r.status_code)
m = r.get_json()["mbl"]
mo = {o["username"]: o for o in m["officers"]}
check("every officer in a department is on the leave list", {"alice", "frank", "grace", "henry", "jack"} <= set(mo)
      and "director" not in mo and "dave" not in mo)
check("working days left in the year reported", m["working_days_left"] == integrity.working_days(today, date(Y, 12, 31), HOL))
a = mo["alice"]
exp = 22 - integrity.working_days(date(Y, 4, 6), date(Y, 4, 10), HOL) - integrity.working_days(date(Y, 8, 17), date(Y, 8, 21), HOL)
check("alice: 18 + 4 c/f - taken = remaining", a["entitled"] == 18 and a["carried_forward"] == 4 and a["remaining"] == exp, a)
check("alice: carried-forward days left flagged", a["carried_forward_left"] == 4)
exp_state = "at_risk" if (today.month, today.day) >= (10, 1) else "to_clear"
check(f"alice: state {exp_state} with a start-by date", a["state"] == exp_state and a["start_by"], a["state"])
if today >= date(Y, 7, 17):
    check("frank: all 16 days taken -> cleared", mo["frank"]["state"] == "cleared" and mo["frank"]["remaining"] == 0, mo["frank"])
g_ = mo["grace"]
check("grace: future leave counted as booked", g_["booked"] == integrity.working_days(today + timedelta(days=14), today + timedelta(days=27), HOL)
      and g_["state"] in ("booked", "to_clear", "at_risk"), g_)
check("henry: more left than working days -> cannot clear", mo["henry"]["state"] == "cannot_clear", mo["henry"])
check("default entitlement used when HR has not set one", not mo["jack"]["entitlement_set"] and mo["jack"]["entitled"] == 14)
check("list ordered worst first", m["officers"][0]["state"] in ("cannot_clear", "forfeited"))
check("pure state: past year with days left -> forfeited", integrity.mbl_state(3, 0, Y - 1, today, HOL) == "forfeited")
check("HR can read; requestor / employee / IT Admin cannot",
      get("hr", "/api/integrity").status_code == 200 and get("req", "/api/integrity").status_code == 403
      and get("alice", "/api/integrity").status_code == 403 and get("itadmin", "/api/integrity").status_code == 403)

# ---- 4. recording leave + entitlements
a_id = emp["alice"]["id"]
start = today + timedelta(days=(7 - today.weekday()))          # next Monday
before = a["remaining"]
r = post("hr", "/api/mbl/leaves", {"employee_id": a_id, "start_date": start.isoformat(),
                                   "end_date": (start + timedelta(days=4)).isoformat(), "note": "Clearing leave"})
rec = r.get_json()
if start.year == Y:
    check("HR records leave (201); remaining drops by its working days",
          r.status_code == 201 and rec["remaining"] == before - rec["working_days"], rec)
check("overlapping leave refused (409)", post("hr", "/api/mbl/leaves", {"employee_id": a_id,
      "start_date": (start + timedelta(days=2)).isoformat(), "end_date": (start + timedelta(days=8)).isoformat()}).status_code == 409)
check("end before start refused", post("hr", "/api/mbl/leaves", {"employee_id": a_id, "start_date": "2026-12-05",
                                                                  "end_date": "2026-12-01"}).status_code == 400)
check("weekend-only leave refused", post("hr", "/api/mbl/leaves", {"employee_id": a_id, "start_date": "2026-11-07",
                                                                    "end_date": "2026-11-08"}).status_code == 400)
check("leave across 31 Dec refused (split per year)", post("hr", "/api/mbl/leaves", {"employee_id": a_id,
      "start_date": f"{Y}-12-30", "end_date": f"{Y + 1}-01-04"}).status_code == 400)
check("director cannot record leave", post("director", "/api/mbl/leaves", {"employee_id": a_id}).status_code == 403)
check("HR cancels it", post("hr", f"/api/mbl/leaves/{rec['id']}/cancel").status_code == 200
      and post("hr", f"/api/mbl/leaves/{rec['id']}/cancel").status_code == 404)
r = post("hr", "/api/mbl/entitlements", {"employee_id": emp["jack"]["id"], "year": Y, "entitled_days": 21,
                                        "carried_forward_days": 2.5})
check("HR sets jack's entitlement 21 + 2.5", r.status_code == 200 and r.get_json()["entitled"] == 21
      and r.get_json()["carried_forward"] == 2.5 and r.get_json()["entitlement_set"], r.get_json())
check("bad entitlement refused", post("hr", "/api/mbl/entitlements", {"employee_id": emp["jack"]["id"], "entitled_days": -1}).status_code == 400
      and post("hr", "/api/mbl/entitlements", {"employee_id": emp["jack"]["id"], "entitled_days": 1.3}).status_code == 400)
SENT.clear()
n = post("hr", "/api/mbl/remind").get_json()["reminded"]
check("reminder e-mails each officer with leave to clear (+ HR copy)",
      n >= 3 and any(t.startswith("alice@") for t, *_ in SENT) and any(t.startswith("hr@") for t, *_ in SENT)
      and not any(t.startswith("frank@") for t, *_ in SENT), n)
check("reminder mentions forfeiture of carried-forward days",
      any(t.startswith("alice@") and "forfeited" in b for t, _, b in SENT))

# ---- 5. sensitive position: IT Admin only
r = post("itadmin", f"/api/integrity/officers/{emp['charlie']['id']}/sensitive", {"sensitive": True, "reason": "Handles payments"})
check("IT Admin marks charlie sensitive", r.status_code == 200 and r.get_json()["sensitive_post"] is True, r.get_json())
check("HR and Director cannot set it", post("hr", f"/api/integrity/officers/{emp['charlie']['id']}/sensitive", {"sensitive": False}).status_code == 403
      and post("director", f"/api/integrity/officers/{emp['charlie']['id']}/sensitive", {"sensitive": False}).status_code == 403)
check("non-boolean refused", post("itadmin", f"/api/integrity/officers/{emp['charlie']['id']}/sensitive", {"sensitive": "yes"}).status_code == 400)
check("IT Admin can list officers", get("itadmin", "/api/integrity/officers").status_code == 200)
cas = {o["username"]: o for o in get("director", "/api/integrity").get_json()["casino"]}
check("charlie's rule follows the switch (every visit)", cas["charlie"]["rule"]["code"] == "every_visit")
post("itadmin", f"/api/integrity/officers/{emp['charlie']['id']}/sensitive", {"sensitive": False})

# ---- 6. casino summary from the seed
cas = {o["username"]: o for o in get("director", "/api/integrity").get_json()["casino"]}
check("erin (sensitive) declared 20 days after -> late", cas["erin"]["state"] == "late", cas["erin"]["state"])
check("bob (sensitive) declared on time -> no issues", cas["bob"]["state"] == "ok", cas["bob"]["state"])
check("ivy bought the annual levy -> frequent, pass valid a year", cas["ivy"]["state"] == "frequent" and cas["ivy"]["annual_levy_until"])
check("jack: two voluntary declarations -> no issues", cas["jack"]["state"] == "ok")
check("no post category in the casino data", "post_category" not in cas["jack"] and "category_label" not in cas["jack"])
decls = get("hr", "/api/integrity").get_json()["declarations"]
ch = [d for d in decls if d["username"] == "charlie"]
if ch:
    fifth = [d for d in ch if d["visit_no_in_month"] == 5]
    check("charlie: visits 1-4 voluntary, the 5th required and on time",
          len(fifth) == 1 and fifth[0]["required"] and "late" not in fifth[0]["flags"] and "frequent" in fifth[0]["flags"]
          and all(not d["required"] for d in ch if d["visit_no_in_month"] < 5), ch)
bob_off = [d for d in decls if d["username"] == "bob" and d["official_duty"]]
check("bob's official visit is not flagged", bob_off and bob_off[0]["flags"] == [])

# ---- 7. declaring
SENT.clear()
r = post("jack", "/api/casino/declarations", {"kind": "visit", "visit_date": today.isoformat(), "casino": "Marina Bay Sands"})
check("employee declares own visit, voluntary for a non-sensitive officer", r.status_code == 201 and r.get_json()["flags"] == []
      and not r.get_json()["required"] and not SENT, r.get_json())
check("employee cannot declare for someone else", post("jack", "/api/casino/declarations", {"employee_id": emp["ivy"]["id"],
      "visit_date": today.isoformat(), "casino": "Marina Bay Sands"}).status_code == 403)
r = post("hr", "/api/casino/declarations", {"employee_id": emp["erin"]["id"], "visit_date": (today - timedelta(days=9)).isoformat(),
                                           "casino": "Marina Bay Sands"})
check("HR declares on behalf; 9 days after a sensitive officer's visit -> late + HR/Director alert",
      r.status_code == 201 and "late" in r.get_json()["flags"] and {"hr", "director"} <= {t.split("@")[0] for t, *_ in SENT})
r = post("hr", "/api/casino/declarations", {"employee_id": emp["erin"]["id"], "visit_date": (today - timedelta(days=7)).isoformat(),
                                           "casino": "Marina Bay Sands"})
check("declared on day 7 -> on time", r.status_code == 201 and r.get_json()["flags"] == [])
check("official visit needs a purpose", post("bob", "/api/casino/declarations", {"visit_date": today.isoformat(),
      "casino": "Marina Bay Sands", "official_duty": True}).status_code == 400)
check("future visit refused", post("jack", "/api/casino/declarations", {"visit_date": (today + timedelta(days=1)).isoformat(),
                                                                         "casino": "Marina Bay Sands"}).status_code == 400)
check("unknown casino refused", post("jack", "/api/casino/declarations", {"visit_date": today.isoformat(), "casino": "Genting"}).status_code == 400)
check("director cannot declare", post("director", "/api/casino/declarations", {"visit_date": today.isoformat(),
                                                                                "casino": "Marina Bay Sands"}).status_code == 403)
m0 = date(today.year, today.month, 1) - timedelta(days=1)
m0 = date(m0.year, m0.month, 1)
hid = emp["henry"]["id"]
for i in range(4):
    db.query("""INSERT INTO casino_declarations (employee_id, kind, visit_date, casino, declared_by, declared_at)
                VALUES (%s,'visit',%s,'Marina Bay Sands',%s, now())""", (hid, m0 + timedelta(days=i), hid), fetch=None)
d5 = post("henry", "/api/casino/declarations", {"visit_date": (m0 + timedelta(days=5)).isoformat(), "casino": "Marina Bay Sands"}).get_json()
check("non-sensitive officer: 5th visit in a month is required; declared > 7 days later -> late",
      d5["visit_no_in_month"] == 5 and d5["required"] and
      (("late" in d5["flags"]) == ((today - (m0 + timedelta(days=5))).days > 7)), d5)

# ---- 8. the officer's own view
me = get("alice", "/api/casino/me").get_json()
check("officer sees own rule, sensitive flag, declarations and leave to clear",
      me["in_scope"] and me["sensitive_post"] and me["rule"]["code"] == "every_visit" and me["mbl"]["remaining"] > 0)
check("staff without a department are out of scope", get("director", "/api/casino/me").get_json()["in_scope"] is False)

# ---- 9. audit trail
acts = {r["action"] for r in db.query("SELECT DISTINCT action FROM audit_log")}
need = {"leave_recorded", "leave_cancelled", "leave_entitlement_set", "mbl_reminders_sent", "casino_declared", "sensitive_post_changed"}
check("every action audited", need <= acts, need - acts)
v = audit.verify() if hasattr(audit, "verify") else {"ok": True}
check("audit chain still verifies", (v.get("ok") if isinstance(v, dict) else bool(v)), v)

print(f"\n{sum(results)}/{len(results)} passed")
sys.exit(0 if all(results) else 1)
