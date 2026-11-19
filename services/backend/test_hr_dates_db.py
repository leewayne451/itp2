"""Database checks for dated HR events (CHANGE 013): transfer date, last working day and
last physical day.

Runs the real backend (HR webhook included) against a THROWAWAY Postgres, with Keycloak,
tokens and e-mail stubbed. It changes the demo data, so never point it at the POC's
database. Same set-up as test_scheduling_db.py:

    cd services/backend
    SCHEDULING_TEST_DB=yes PGHOST=127.0.0.1 PGPORT=5433 PGUSER=postgres PGDATABASE=sj_test \
      KEYCLOAK_BACKEND_CLIENT_SECRET=kkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkk AUDIT_HMAC_KEY=aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa \
      AUDIT_LOG_DIR=/tmp/sj-audit python test_hr_dates_db.py
"""
import json, os, pathlib, sys, time, types, threading, uuid
from datetime import datetime, timedelta, timezone
if os.environ.get("SCHEDULING_TEST_DB") != "yes":
    sys.exit("Refusing to run: set SCHEDULING_TEST_DB=yes and point PG* at a throwaway database.")
os.environ.setdefault("HR_WEBHOOK_SECRET", "w" * 40)
sys.path.insert(0, str(pathlib.Path(__file__).parent))
from app import create_app, bootstrap, tokens, sync, db, core, scheduling, offboarding, workflows
from app import tracker_logic as tl
from app.hr_events import sign

core.send_email = scheduling.send_email = lambda *a, **k: True
from app.api import hr as hr_api
hr_api.send_email = lambda *a, **k: True


class _Now:   # run "background" threads inline
    def __init__(self, target, args=(), daemon=None, **kw):
        self.target, self.args = target, args

    def start(self):
        self.target(*self.args)


core.threading = types.SimpleNamespace(Thread=_Now)
scheduling.threading = types.SimpleNamespace(Thread=_Now, Lock=threading.Lock)
sync.push = lambda *a, **k: True
sync.sync_employee = lambda *a, **k: True
tokens.validate = lambda tok: {"sub": tok, "iat": 4_000_000_000, "amr": ["pwd", "otp"]}

bootstrap()
db.query("UPDATE employees SET keycloak_id = username, status = CASE WHEN role='system' THEN status ELSE 'active' END", fetch=None)
app = create_app(); c = app.test_client()
failed = []
SECRET = os.environ["HR_WEBHOOK_SECRET"]
TODAY = datetime.now(tl.SGT).date()


def check(name, ok, extra=""):
    print(("PASS  " if ok else "FAIL  ") + name + (f"   [{extra}]" if not ok and extra else ""))
    if not ok:
        failed.append(name)


def call(m, path, user, body=None):
    r = c.open(path, method=m, json=body, headers={"Authorization": f"Bearer {user}"})
    return r.status_code, r.get_json()


def hr_event(etype, username, **fields):
    body = json.dumps({"event_id": f"hr-{uuid.uuid4()}", "type": etype, "employee": {"username": username},
                       **fields}).encode()
    ts = str(int(time.time()))
    r = c.post("/api/hr/events", data=body, headers={"Content-Type": "application/json", "X-HR-Timestamp": ts,
                                                       "X-HR-Signature": sign(SECRET, ts, body)})
    return r.status_code, r.get_json()


def emp(u):
    return db.query("SELECT * FROM employees WHERE username=%s", (u,), fetch="one")


def req(rid):
    return db.query("SELECT * FROM requests WHERE id=%s", (rid,), fetch="one")


def approve(rid):
    for u in ("hr", "director", "aa"):
        s, j = call("POST", f"/api/requests/{rid}/approve", u)
        assert s == 200, (u, s, j)


def other_dept(e):
    return db.query("SELECT name FROM departments WHERE id<>%s ORDER BY id LIMIT 1", (e["department_id"],), fetch="one")["name"]


def next_weekday(wd, at_least):
    d = TODAY + timedelta(days=at_least)
    while d.weekday() != wd:
        d += timedelta(days=1)
    return d


# ---------------------------------------------------------- 1. dated transfer
erin = emp("erin")
when = TODAY + timedelta(days=3)
s, j = hr_event("transfer", "erin", department=other_dept(erin), effective_date=when.isoformat())
check("transfer with a date accepted", s == 200 and j.get("request_type") == "shift", j)
rid = j["request_id"]
check("transfer date stored on the shift", req(rid)["effective_date"] == when)
check("employee keeps access until the date", emp("erin")["status"] == "active")
approve(rid)
s, j = call("POST", f"/api/requests/{rid}/process", "itadmin", {"override_reason": "test"})
check("IT cannot process before the transfer date", s == 409 and "takes effect" in j["error"], (s, j))
check("worker does nothing before the date", workflows.start_due_transfers(TODAY) == [] and emp("erin")["status"] == "active")
check("worker suspends on the date", workflows.start_due_transfers(when) == [rid] and emp("erin")["status"] == "suspended")
check("worker suspends once", workflows.start_due_transfers(when) == [])
ev = db.query("SELECT action FROM request_events WHERE request_id=%s ORDER BY id", (rid,))
check("timeline shows the transfer start", "transfer_started" in [e["action"] for e in ev])
t = scheduling.tracker(rid)
check("tracker summary names the transfer date", f"{when.day} {when:%b}" in t["summary"], t["summary"])
db.query("UPDATE requests SET effective_date=%s WHERE id=%s", (TODAY, rid), fetch=None)   # the day has come
s, j = call("POST", f"/api/requests/{rid}/process", "itadmin", {"override_reason": "test"})
check("IT processes on the date: moved and active again",
      s == 200 and emp("erin")["status"] == "active" and emp("erin")["department_id"] == req(rid)["target_department_id"], (s, j))
check("worker leaves a processed transfer alone", workflows.start_due_transfers(when) == [] and emp("erin")["status"] == "active")

grace = emp("grace")
s, j = hr_event("transfer", "grace", department=other_dept(grace), effective_date=TODAY.isoformat())
check("transfer dated today suspends at once (as before)", s == 200 and emp("grace")["status"] == "suspended", j)
s, j = call("POST", f"/api/requests/{j['request_id']}/reject", "hr", {"reason": "test"})
check("rejecting it lifts the suspension", emp("grace")["status"] == "active")

henry = emp("henry")
s, j = hr_event("transfer", "henry", department=other_dept(henry), effective_date=(when + timedelta(days=1)).isoformat())
s, j = call("POST", f"/api/requests/{j['request_id']}/reject", "hr", {"reason": "test"})
check("rejecting a future transfer never suspends", emp("henry")["status"] == "active"
      and workflows.start_due_transfers(when + timedelta(days=5)) == [])

# ---------------------------------------------------------- 2. resignation: two dates
ACCOUNT = core.service_by_type("account")
for u in ("frank", "ivy"):             # leavers hold an account, so there is something to return
    if not core.has_access(emp(u)["id"], ACCOUNT["id"]):
        core.grant_access(emp(u)["id"], ACCOUNT["id"])
sat = next_weekday(5, 8)            # a Saturday at least a week away
fri = sat - timedelta(days=1)
s, j = hr_event("resign", "frank", effective_date=sat.isoformat(), reason="Resignation")
check("resignation accepted and scheduled", s == 200 and j.get("scheduled"), j)
check("last physical day defaults to the Friday", j.get("last_physical_day") == fri.isoformat(), j)
cut = emp("frank")["scheduled_offboard_at"].astimezone(tl.SGT)
check("access cut at 00:00 SGT after the last working day",
      cut.date() == sat + timedelta(days=1) and cut.strftime("%H:%M:%S") == "00:00:00", cut)
check("still active until then", emp("frank")["status"] == "active")
ret = j.get("return_request")
check("Return request raised straight away, due by the last physical day",
      ret and req(ret)["type"] == "return" and req(ret)["return_by"] == fri, j)
approve(ret)
s, j = call("POST", f"/api/requests/{ret}/process", "itadmin")
check("Return auto-booked", s == 200 and j["status"] == "pending_euss", (s, j))
b = scheduling.live_booking(ret)
check("appointment on or before the last physical day", b and b["starts_at"].astimezone(tl.SGT).date() <= fri, b)
late_slot = db.query("""SELECT id FROM euss_slots WHERE status='open' AND starts_at >= %s ORDER BY starts_at LIMIT 1""",
                     (scheduling.deadline(req(ret)),), fetch="one")
s, j = call("POST", f"/api/requests/{ret}/reschedule", "euss", {"slot_id": late_slot["id"]})
check("moving it past the last physical day is refused", s == 409 and "last physical day" in j["error"], (s, j))
s, j = call("GET", f"/api/requests/{ret}/slots", "euss")
check("slot list only offers slots up to the last physical day",
      all(datetime.fromisoformat(x["starts_at"]).astimezone(tl.SGT).date() <= fri for x in j["slots"]), j)
t = scheduling.tracker(ret)
check("tracker says return by the last physical day",
      any("last physical day" in (x["note"] or "") for x in t["steps"]) and f"by {fri:%a}" in t["summary"], t["summary"])

db.query("UPDATE employees SET scheduled_offboard_at=now()-interval '1 minute' WHERE username='frank'", fetch=None)
offboarding.execute_due()
returns = db.query("SELECT id FROM requests WHERE target_employee_id=%s AND type='return'", (emp("frank")["id"],))
check("cut-off runs and reuses the early Return (no second one)",
      emp("frank")["status"] == "inactive" and [r["id"] for r in returns] == [ret], returns)

s, j = hr_event("resign", "ivy", effective_date=next_weekday(2, 8).isoformat(),
                last_physical_day=next_weekday(1, 8).isoformat())
check("explicit last physical day kept", s == 200 and req(j["return_request"])["return_by"] == next_weekday(1, 8), j)
ivy_ret = j["return_request"]
s, j = hr_event("cancel_resignation", "ivy", reason="Resignation withdrawn")
check("cancelling the resignation withdraws the early Return",
      s == 200 and j.get("withdrawn_requests") == [ivy_ret] and req(ivy_ret)["status"] == "rejected"
      and emp("ivy")["scheduled_offboard_at"] is None and emp("ivy")["status"] == "active", j)

s, j = hr_event("resign", "jack", effective_date=fri.isoformat(), last_physical_day=sat.isoformat())
check("last physical day after the last working day refused", s == 400 and emp("jack")["scheduled_offboard_at"] is None, (s, j))

print()
print(f"{len(failed)} failed" if failed else "all integration checks passed")
raise SystemExit(1 if failed else 0)
