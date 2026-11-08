"""
HRMS — a small stand-in for the HR system of record (or ServiceNow HRSD).  CHANGE 008.

HR records a hire, a transfer or a resignation here. The HRMS keeps its own record of
people and events, then sends the event to the ServiceJourney backend as a SIGNED webhook:

    POST {BACKEND_URL}/api/hr/events
    X-HR-Timestamp: <unix seconds>
    X-HR-Signature: sha256=HMAC_SHA256(HRMS_WEBHOOK_SECRET, "<timestamp>." + body)

The backend answers with what it did (raised an onboard / shift / return request, or
refused the event). Delivery is at-least-once: a 5xx, a timeout or an unreachable backend
is retried in the background with the SAME event id (the backend processes each id once),
a 4xx is final. Failed deliveries can also be re-sent by hand from the page.

This container has no sign-in of its own: it is a demo tool, published on 127.0.0.1 only,
and it refuses cross-site POSTs (a web page cannot make your browser file a resignation).
"""
import hashlib
import hmac
import json
import os
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

import requests
from flask import Flask, abort, redirect, render_template, request, url_for, flash

SGT = timezone(timedelta(hours=8), "SGT")

BACKEND_URL = os.environ.get("BACKEND_URL", "https://backend:5000").rstrip("/")
WEBHOOK_SECRET = os.environ.get("HRMS_WEBHOOK_SECRET", "")
DATA_FILE = os.environ.get("HRMS_DATA_FILE", "/data/hrms.json")
DEPARTMENTS = [d.strip() for d in os.environ.get("HRMS_DEPARTMENTS", "Engineering,Sales,Human Resources").split(",") if d.strip()]
CA_BUNDLE = os.environ.get("CA_BUNDLE", "/certs/ca.crt")
TLS_CERT = os.environ.get("TLS_CERT", "/certs/server.crt")
TLS_KEY = os.environ.get("TLS_KEY", "/certs/server.key")
RETRY_INTERVAL = int(os.environ.get("HRMS_RETRY_SECONDS", "15"))
MAX_ATTEMPTS = int(os.environ.get("HRMS_MAX_ATTEMPTS", "10"))

if len(WEBHOOK_SECRET) < 32:
    raise SystemExit("HRMS_WEBHOOK_SECRET is missing or too short. Run `python scripts/generate_secrets.py`, "
                     "then `docker compose up -d --force-recreate`.")

app = Flask(__name__)
app.secret_key = hashlib.sha256(b"hrms-flash|" + WEBHOOK_SECRET.encode()).digest()   # flash messages only
_lock = threading.RLock()

# The same people the ServiceJourney demo database starts with (username, name, department).
SEED_PEOPLE = [
    ("alice", "Alice Employee", "Engineering"), ("erin", "Erin Engineer", "Engineering"),
    ("frank", "Frank Engineer", "Engineering"), ("bob", "Bob Employee", "Sales"),
    ("grace", "Grace Sales", "Sales"), ("henry", "Henry Sales", "Sales"),
    ("charlie", "Charlie Employee", "Human Resources"), ("ivy", "Ivy HR", "Human Resources"),
    ("jack", "Jack HR", "Human Resources"),
]


# --------------------------------------------------------------------------- #
# The HR record (people + events), a JSON file on the container's volume
# --------------------------------------------------------------------------- #
def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _load():
    try:
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, ValueError):
        people = [{"username": u, "full_name": n, "email": f"{u}@servicejourney.local", "department": d,
                   "status": "active"} for u, n, d in SEED_PEOPLE]
        return {"people": people, "events": []}


def _save(db):
    os.makedirs(os.path.dirname(DATA_FILE) or ".", exist_ok=True)
    tmp = DATA_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(db, f, indent=1)
    os.replace(tmp, DATA_FILE)


STATE = _load()


def _person(username):
    return next((p for p in STATE["people"] if p["username"] == username), None)


# --------------------------------------------------------------------------- #
# Delivery
# --------------------------------------------------------------------------- #
def _verify():
    return CA_BUNDLE if os.path.exists(CA_BUNDLE) else True


def _deliver(ev):
    """One delivery attempt. Updates ev in place (caller holds the lock and saves)."""
    ts = str(int(time.time()))
    body = ev["body"].encode()
    sig = "sha256=" + hmac.new(WEBHOOK_SECRET.encode(), f"{ts}.".encode() + body, hashlib.sha256).hexdigest()
    ev["attempts"] += 1
    ev["last_attempt"] = _now()
    try:
        r = requests.post(f"{BACKEND_URL}/api/hr/events", data=body, timeout=15, verify=_verify(),
                          headers={"Content-Type": "application/json", "X-HR-Timestamp": ts,
                                   "X-HR-Signature": sig, "X-HR-Event-Id": ev["event_id"]})
        try:
            answer = r.json()
        except ValueError:
            answer = {"error": r.text[:200]}
        ev["http_status"] = r.status_code
        ev["response"] = answer
        if r.status_code == 200:
            ev["delivery"] = "delivered"
        elif r.status_code == 409 and answer.get("status") == "received":
            ev["delivery"] = "pending"          # the backend is still working on it: ask again later
        elif 400 <= r.status_code < 500:
            ev["delivery"] = "refused"          # final: retrying the same event cannot help
        else:
            ev["delivery"] = "pending"          # 5xx: the backend failed or is not ready; retry
    except requests.RequestException as e:
        ev["http_status"] = None
        ev["response"] = {"error": f"backend unreachable ({type(e).__name__})"}
        ev["delivery"] = "pending"
    if ev["delivery"] == "pending" and ev["attempts"] >= MAX_ATTEMPTS:
        ev["delivery"] = "failed"               # gave up; "Re-send" on the page tries again


def _retry_loop():
    while True:
        time.sleep(RETRY_INTERVAL)
        try:
            with _lock:
                for ev in STATE["events"]:
                    if ev["delivery"] == "pending":
                        _deliver(ev)
                        _save(STATE)
        except Exception as e:  # noqa: BLE001 — never let the loop die
            print(f"[hrms] retry error: {type(e).__name__}: {e}", flush=True)


# --------------------------------------------------------------------------- #
# Recording an event
# --------------------------------------------------------------------------- #
def _record(etype, person, department=None, reason=None):
    """Update the HR record, then send the event. Returns the event dict."""
    payload = {
        "event_id": f"hr-{uuid.uuid4()}",
        "type": etype,
        "occurred_at": _now(),
        "employee": {"username": person["username"], "full_name": person["full_name"], "email": person["email"]},
        "department": department,
        "effective_date": datetime.now(SGT).date().isoformat(),
        "reason": reason or None,
    }
    ev = {"event_id": payload["event_id"], "type": etype, "username": person["username"],
          "full_name": person["full_name"], "department": department, "recorded": payload["occurred_at"],
          "body": json.dumps(payload, separators=(",", ":"), sort_keys=True),
          "delivery": "pending", "attempts": 0, "http_status": None, "response": None, "last_attempt": None}
    with _lock:
        STATE["events"].insert(0, ev)
        del STATE["events"][200:]
        _deliver(ev)
        _save(STATE)
    return ev


def _flash_result(ev):
    resp = ev["response"] or {}
    if ev["delivery"] == "delivered":
        bits = []
        if resp.get("kill_switch"):
            bits.append("kill switch applied — suspended everywhere")
        if resp.get("employee_created"):
            bits.append("employee created")
        if resp.get("request_id"):
            bits.append(f"{resp['request_type']} request #{resp['request_id']} raised")
        elif resp.get("return_note"):
            bits.append(resp["return_note"])
        flash(f"{ev['type'].title()} recorded and delivered: " + ("; ".join(bits) or "done") + ".", "ok")
    elif ev["delivery"] == "refused":
        flash(f"{ev['type'].title()} recorded in HR, but ServiceJourney refused it: {resp.get('error', 'unknown reason')}.", "error")
    else:
        flash(f"{ev['type'].title()} recorded in HR; delivery to ServiceJourney is pending "
              f"({(resp or {}).get('error', 'waiting')}). It will be retried automatically.", "warn")


# --------------------------------------------------------------------------- #
# Web UI
# --------------------------------------------------------------------------- #
@app.before_request
def _same_origin_only():
    """A POST must come from this page: compares Origin (or Referer) with the Host the
    browser used. Stops another website making your browser file an HR event."""
    if request.method != "POST":
        return None
    source = request.headers.get("Origin") or request.headers.get("Referer") or ""
    if not source or urlparse(source).netloc != request.host:
        abort(403, "Cross-site request refused")


@app.after_request
def _headers(resp):
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["Cache-Control"] = "no-store"
    resp.headers["Referrer-Policy"] = "same-origin"
    return resp


@app.template_filter("sgt")
def _sgt(iso):
    if not iso:
        return ""
    return datetime.fromisoformat(iso).astimezone(SGT).strftime("%d %b %Y %H:%M:%S")


@app.get("/")
def index():
    with _lock:
        people = sorted(STATE["people"], key=lambda p: (p["status"] != "active", p["full_name"]))
        events = list(STATE["events"][:50])
    return render_template("index.html", people=people, events=events, departments=DEPARTMENTS, backend=BACKEND_URL)


@app.post("/hire")
def hire():
    f = request.form
    username = f.get("username", "").strip().lower()
    full_name = f.get("full_name", "").strip()
    department = f.get("department", "").strip()
    email = f.get("email", "").strip() or f"{username}@servicejourney.local"
    if not username or not full_name or department not in DEPARTMENTS:
        flash("Username, full name and a department are required.", "error")
        return redirect(url_for("index"))
    with _lock:
        existing = _person(username)
        if existing and existing["status"] == "active":
            flash(f"{username} is already an active employee in HR.", "error")
            return redirect(url_for("index"))
        person = existing or {"username": username}
        person.update(full_name=full_name, email=email, department=department, status="active")
        if not existing:
            STATE["people"].append(person)
        _save(STATE)
    _flash_result(_record("hire", person, department, f.get("reason")))
    return redirect(url_for("index"))


@app.post("/transfer")
def transfer():
    f = request.form
    department = f.get("department", "").strip()
    with _lock:
        person = _person(f.get("username", ""))
        if not person or person["status"] != "active" or department not in DEPARTMENTS:
            flash("Pick an active employee and a department.", "error")
            return redirect(url_for("index"))
        if person["department"] == department:
            flash(f"{person['full_name']} is already in {department}.", "error")
            return redirect(url_for("index"))
        person["department"] = department
        _save(STATE)
    _flash_result(_record("transfer", person, department, f.get("reason")))
    return redirect(url_for("index"))


@app.post("/resign")
def resign():
    f = request.form
    with _lock:
        person = _person(f.get("username", ""))
        if not person or person["status"] != "active":
            flash("Pick an active employee.", "error")
            return redirect(url_for("index"))
        person["status"] = "resigned"
        _save(STATE)
    _flash_result(_record("resign", person, None, f.get("reason") or "Resignation"))
    return redirect(url_for("index"))


@app.post("/events/<event_id>/resend")
def resend(event_id):
    with _lock:
        ev = next((e for e in STATE["events"] if e["event_id"] == event_id), None)
        if not ev:
            abort(404)
        if ev["delivery"] == "delivered":
            flash("That event was already delivered.", "warn")
        else:
            ev["attempts"] = 0
            _deliver(ev)
            _save(STATE)
            _flash_result(ev)
    return redirect(url_for("index"))


@app.get("/health")
def health():
    return {"status": "ok"}


def ssl_context():
    if os.path.exists(TLS_CERT) and os.path.exists(TLS_KEY):
        return (TLS_CERT, TLS_KEY)
    print("[tls] certificate not found — serving plain HTTP", flush=True)
    return None


if __name__ == "__main__":
    threading.Thread(target=_retry_loop, name="hrms-retry", daemon=True).start()
    app.run(host="0.0.0.0", port=5000, ssl_context=ssl_context())
