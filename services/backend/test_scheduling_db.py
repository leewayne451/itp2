"""Database checks for smart EUSS booking and the journey tracker (CHANGE 011).

Runs the real backend code against a THROWAWAY Postgres (Keycloak, tokens and e-mail are
stubbed). It changes the demo data heavily, so never point it at the POC's database:

    initdb + start a scratch Postgres, then:
    cd services/backend
    SCHEDULING_TEST_DB=yes PGHOST=127.0.0.1 PGPORT=5433 PGUSER=postgres PGDATABASE=sj_test \
      KEYCLOAK_BACKEND_CLIENT_SECRET=kkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkk AUDIT_HMAC_KEY=aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa \
      AUDIT_LOG_DIR=/tmp/sj-audit python test_scheduling_db.py
"""
import os, pathlib, sys, threading, types
from datetime import datetime, timedelta, timezone
if os.environ.get("SCHEDULING_TEST_DB") != "yes":
    sys.exit("Refusing to run: set SCHEDULING_TEST_DB=yes and point PG* at a throwaway database.")
sys.path.insert(0, str(pathlib.Path(__file__).parent))
from app import create_app, bootstrap, tokens, sync, db, core, scheduling, mailer
from app import tracker_logic as tl

SENT = []
def fake_send(to, subject, body):
    SENT.append((to, subject, body)); return True
core.send_email = fake_send; scheduling.send_email = fake_send


class _Now:   # run "background" e-mail threads inline so the checks can see the e-mails
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
def check(name, ok, extra=""):
    print(("PASS  " if ok else "FAIL  ") + name + (f"   [{extra}]" if not ok and extra else ""))
    if not ok: failed.append(name)
def call(m, path, user, body=None):
    r = c.open(path, method=m, json=body, headers={"Authorization": f"Bearer {user}"})
    return r.status_code, r.get_json()
def emp(u): return db.query("SELECT * FROM employees WHERE username=%s", (u,), fetch="one")
def dept(n): return db.query("SELECT id FROM departments ORDER BY id LIMIT 1 OFFSET %s", (n,), fetch="one")["id"]

def through_it(rid, override="test"):
    for u in ("hr", "director", "aa"):
        s, j = call("POST", f"/api/requests/{rid}/approve", u); assert s == 200, (u, s, j)
    s, j = call("POST", f"/api/requests/{rid}/process", "itadmin", {"override_reason": override})
    assert s == 200, (s, j)
    return j

# ---------------------------------------------------------- 1. onboard + auto-match
dave = emp("dave")
s, j = call("POST", "/api/requests", "req", {"type": "onboard", "target_employee_id": dave["id"], "department_id": dept(0)})
check("onboard raised", s == 201, j); rid = j["id"]
SENT.clear()
j = through_it(rid)
check("IT process -> straight to EUSS (auto-matched)", j["status"] == "pending_euss" and j["appointment_at"], j["status"])
appt = datetime.fromisoformat(j["appointment_at"]).astimezone(tl.SGT)
check("appointment respects 24 h lead time", appt >= datetime.now(timezone.utc) + timedelta(hours=24))
check("appointment inside EUSS hours on a weekday", appt.weekday() < 5 and (9 <= appt.hour < 12 or 14 <= appt.hour < 17), appt)
evs = db.query("SELECT e.action, em.username FROM request_events e LEFT JOIN employees em ON em.id=e.actor_id WHERE request_id=%s ORDER BY e.id", (rid,))
check("timeline: processed then appointment_booked by the scheduler",
      [e["action"] for e in evs][-2:] == ["processed", "appointment_booked"] and evs[-1]["username"] == "appointment-scheduler", evs)
req_mail = [m for m in SENT if m[0] == emp("req")["email"] and "ACTION REQUIRED" in m[1]]
check("requestor NOT asked to book an appointment", not req_mail, req_mail)
dave_mail = [m for m in SENT if m[0] == dave["email"] and "Your EUSS appointment" in m[1]]
check("employee e-mailed when/where/what to bring", dave_mail and "Please bring" in dave_mail[0][2] and "/my-journey" in dave_mail[0][2])
euss_mail = [m for m in SENT if m[0] == emp("euss")["email"] and "ACTION REQUIRED" in m[1]]
check("EUSS told exactly once", len(euss_mail) == 1, len(euss_mail))

# ---------------------------------------------------------- 2. tracker
s, mine = call("GET", "/api/tracker/me", "dave")
check("employee sees own tracker", s == 200 and mine and mine[0]["request_id"] == rid, s)
t = mine[0]
check("tracker headline is the appointment", t["headline"] == "Appointment booked" and t["appointment"]["method"] == "auto", t["headline"])
check("tracker lists what to bring", any("Acceptable Use" in i for i in t["what_to_bring"]))
check("tracker steps all done up to EUSS", [x["state"] for x in t["steps"]][:6] == ["done"] * 6, [x["state"] for x in t["steps"]])
s, _ = call("GET", f"/api/requests/{rid}/tracker", "charlie")
check("another employee cannot see it", s == 403, s)
s, _ = call("GET", f"/api/requests/{rid}/tracker", "hr")
check("staff can see it", s == 200, s)

# ---------------------------------------------------------- 3. reschedule
s, sl = call("GET", f"/api/requests/{rid}/slots", "dave")
check("alternative slots offered", s == 200 and len(sl["slots"]) >= 5, s)
target = sl["slots"][4]
SENT.clear()
s, j = call("POST", f"/api/requests/{rid}/reschedule", "dave", {"slot_id": target["id"]})
check("employee moves the appointment", s == 200, j)
to_dave = [m[1] for m in SENT if m[0] == dave["email"]]
check("a move sends the employee exactly one e-mail", len(to_dave) == 1 and "Your EUSS appointment" in to_dave[0], to_dave)
check("a move does not re-alert EUSS", not [m for m in SENT if m[0] == emp("euss")["email"]], SENT)
s, j = call("POST", f"/api/requests/{rid}/reschedule", "dave", {"slot_id": target["id"]})
check("moving to the slot it already has is refused (double click / resubmit)", s == 409, (s, j))
r = core.get_request(rid)
check("request appointment_at updated", r["appointment_at"].isoformat() == datetime.fromisoformat(target["starts_at"]).isoformat())
hist = db.query("SELECT status, method FROM euss_bookings WHERE request_id=%s ORDER BY id", (rid,))
check("booking history: auto (moved) -> chosen (booked)", [(h["status"], h["method"]) for h in hist] == [("moved", "auto"), ("booked", "chosen")], hist)
s, _ = call("POST", f"/api/requests/{rid}/reschedule", "charlie", {"slot_id": sl["slots"][5]["id"]})
check("someone else cannot move it", s == 403, s)
too_soon = db.query("INSERT INTO euss_slots (starts_at, ends_at, location, capacity, source) VALUES (now()+interval '3 hours', now()+interval '210 minutes', 'X', 1, 'manual') RETURNING id", fetch="one")["id"]
s, j = call("POST", f"/api/requests/{rid}/reschedule", "dave", {"slot_id": too_soon})
check("cannot pick a slot inside the preparation time", s == 409, j)

# ---------------------------------------------------------- 4. block -> rematch
SENT.clear()
cur_slot = scheduling.live_booking(rid)["slot_id"]
s, _ = call("POST", f"/api/euss/slots/{cur_slot}/block", "euss", {})
check("block needs a reason", s == 400, s)
s, j = call("POST", f"/api/euss/slots/{cur_slot}/block", "euss", {"reason": "Staff training"})
check("EUSS blocks the slot; booking moved", s == 200 and j["moved"] == [rid], j)
nb = scheduling.live_booking(rid)
check("moved to another open slot", nb["slot_id"] != cur_slot and nb["method"] == "rematched")
check("employee told about the move", any(m[0] == dave["email"] and "Your EUSS appointment" in m[1] for m in SENT))
s, j = call("POST", f"/api/euss/slots/{cur_slot}/reopen", "euss", {})
check("EUSS re-opens it", s == 200)
s, j = call("GET", "/api/euss/slots?days=21", "euss")
booked_rows = [x for x in j["slots"] if x["bookings"]]
check("slot board shows the booking", any(b["request_id"] == rid for x in booked_rows for b in x["bookings"]))

# ---------------------------------------------------------- 5. last place race
one = db.query("INSERT INTO euss_slots (starts_at, ends_at, location, capacity, source) VALUES (now()+interval '30 days', now()+interval '30 days 30 minutes', 'RaceDesk', 1, 'manual') RETURNING id", fetch="one")["id"]
erin, frank = emp("erin"), emp("frank")
rids = []
svc = db.query("SELECT id FROM services WHERE resource_type='account'", fetch="one")["id"]
for e in (erin, frank):
    s, j = call("POST", "/api/requests", "req", {"type": "grant", "target_employee_id": e["id"], "service_id": svc})
    assert s == 201, j; rids.append(j["id"]); through_it(j["id"])
results = []
def grab(r):
    try:
        scheduling._book(r, one, "chosen", None, "race"); results.append("ok")
    except scheduling.SlotUnavailable:
        results.append("full")
ths = [threading.Thread(target=grab, args=(r,)) for r in rids]
[t.start() for t in ths]; [t.join() for t in ths]
check("two requests racing for the last place: one wins", sorted(results) == ["full", "ok"], results)
taken = db.query("SELECT COUNT(*) n FROM euss_bookings WHERE slot_id=%s AND status='booked'", (one,), fetch="one")["n"]
check("slot never over capacity", taken == 1, taken)

# ---------------------------------------------------------- 6. bundling
# whichever of the two racers is still in a roster slot (the winner sits in the odd RaceDesk slot)
first = next(r for r in rids if scheduling.live_booking(r)["slot_id"] != one)
person = core.get_request(first)["target_employee_id"]
s, j = call("POST", "/api/requests", "req", {"type": "grant", "target_employee_id": person,
            "service_id": db.query("SELECT id FROM services WHERE resource_type='laptop'", fetch="one")["id"]})
r2 = j["id"]; through_it(r2)
b1, b2 = scheduling.live_booking(first), scheduling.live_booking(r2)
check("second request for the same person bundled next to the first",
      b2["starts_at"] == b1["ends_at"] or b2["ends_at"] == b1["starts_at"], (b1["starts_at"], b2["starts_at"], b2["reason"]))

# ---------------------------------------------------------- 7. complete -> booking done
s, j = call("POST", f"/api/requests/{rid}/provision", "euss", {"asset_tags": {}})
if s != 200:  # pick tags if needed
    plan = {}
    for kind in ("Laptop", "Account"):
        a = db.query("SELECT asset_tag FROM assets WHERE kind=%s AND status='available' LIMIT 1", (kind,), fetch="one")
        if a: plan[kind] = a["asset_tag"]
    s, j = call("POST", f"/api/requests/{rid}/provision", "euss", {"asset_tags": plan})
check("EUSS provisions", s == 200, j)
check("booking closed as done", db.query("SELECT status FROM euss_bookings WHERE request_id=%s ORDER BY id DESC LIMIT 1", (rid,), fetch="one")["status"] == "done")
s, mine = call("GET", "/api/tracker/me", "dave")
check("tracker shows done", mine[0]["headline"] == "Done" and mine[0]["progress_pct"] == 100, mine[0]["headline"])

# ---------------------------------------------------------- 8. no free slot -> manual
db.query("UPDATE euss_slots SET status='blocked' WHERE status='open'", fetch=None)
scheduling._roster["at"] = 9e18     # stop the roster top-up re-opening anything
grace = emp("grace")
s, j = call("POST", "/api/requests", "req", {"type": "grant", "target_employee_id": grace["id"], "service_id": svc})
r3 = j["id"]; SENT.clear(); j = through_it(r3)
check("no free slot -> waits for the requestor", j["status"] == "pending_appointment", j["status"])
check("requestor asked to book", any("ACTION REQUIRED" in m[1] and m[0] == emp("req")["email"] for m in SENT))
s, t = call("GET", f"/api/requests/{r3}/tracker", "grace")
check("tracker: waiting on the requestor", s == 200 and t["headline"].startswith("Waiting on"), t.get("headline"))
s, j = call("POST", f"/api/requests/{r3}/appointment", "req", {"auto": True})
check("requestor auto-book with nothing free -> 409", s == 409, (s, j))
open_id = db.query("UPDATE euss_slots SET status='open' WHERE id=(SELECT id FROM euss_slots WHERE starts_at > now()+interval '2 days' AND location<>'RaceDesk' ORDER BY starts_at LIMIT 1) RETURNING id", fetch="one")["id"]
s, j = call("POST", f"/api/requests/{r3}/appointment", "req", {"slot_id": open_id})
check("requestor books a chosen slot", s == 200 and j["status"] == "pending_euss", (s, j))
# legacy free-text time still accepted
db.query("UPDATE requests SET status='pending_appointment', appointment_at=NULL WHERE id=%s", (r3,), fetch=None)
db.query("UPDATE euss_bookings SET status='moved' WHERE request_id=%s", (r3,), fetch=None)
s, j = call("POST", f"/api/requests/{r3}/appointment", "req", {"appointment_at": "2030-01-07T10:00:00+08:00"})
check("legacy appointment_at still works", s == 200 and j["status"] == "pending_euss", (s, j))
s, t = call("GET", f"/api/requests/{r3}/tracker", "grace")
check("tracker handles a booking made outside the slots", s == 200 and t["appointment"]["method"] == "manual")

# ---------------------------------------------------------- 9. closed request frees the place
b = scheduling.live_booking(rids[1]) or scheduling.live_booking(rids[0])
slot_id = b["slot_id"]
before = [x for x in db.query(scheduling._FREE_SQL.format(extra=""), (-1, b["starts_at"], b["starts_at"] + timedelta(minutes=1))) if x["id"] == slot_id][0]["free"]
db.query("UPDATE requests SET status='rejected' WHERE id=%s", (b["request_id"],), fetch=None)
after = [x for x in db.query(scheduling._FREE_SQL.format(extra=""), (-1, b["starts_at"], b["starts_at"] + timedelta(minutes=1))) if x["id"] == slot_id][0]["free"]
check("a closed request frees its place", after == before + 1, (before, after))

# ---------------------------------------------------------- 10. permissions on EUSS endpoints
s, _ = call("GET", "/api/euss/slots", "req"); check("requestor cannot open the slot board", s == 403)
s, _ = call("POST", "/api/euss/slots", "dave", {"starts_at": "2030-01-01T10:00"}); check("employee cannot add slots", s == 403)
s, j = call("POST", "/api/euss/slots", "euss", {"starts_at": (datetime.now()+timedelta(days=3)).strftime("%Y-%m-%dT10:00"), "capacity": 3})
check("EUSS adds a one-off slot (on top of a roster slot it adds places)", s == 201 and j["capacity"] in (3, 3 + scheduling.DESKS), j)

print(); print(f"{len(failed)} failed" if failed else "all integration checks passed")
raise SystemExit(1 if failed else 0)
