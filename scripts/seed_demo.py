"""Populate a running ServiceJourney stack with demo data — through the REAL workflow.

Every record is created through the backend API, the HR-system webhook (HRMS UI) or
the analytics container, exactly as a user would create it, so:
  * the tamper-evident audit hash chain stays valid,
  * request timelines, e-mails and appointment bookings are generated normally,
  * workflow rules (separation of duties, quotas, document scanning) are enforced.

Run from the repo root against the RUNNING stack, ONCE, on the demo database:

    python scripts/seed_demo.py            # refuses if `req` already has requests
    python scripts/seed_demo.py --force    # seed again anyway (adds MORE data)
    python scripts/seed_demo.py --finish   # only the review decision + HRMS events
                                           # (completes a run that stopped before them)

Afterwards reset the demo account passwords to their defaults:

    python scripts/reset_demo_passwords.py

Note: signing in as the workflow accounts (req, hr, director, aa, itadmin, euss) goes
through Keycloak exactly like a browser, so this script REPLACES those accounts'
passwords and authenticators (via _e2e.prepare) — hence the password reset afterwards.
Needs: pip install requests pyotp
"""
import base64
import datetime as dt
import os
import sys
import time

import requests

from _e2e import (CA, OIDC, OIDC_SECRETS, ROOT, claims, get, http, new_passphrase, post, prepare,
                  tokens_for)

SGT = dt.timezone(dt.timedelta(hours=8))
HRMS = os.environ.get("HRMS_URL", "https://localhost:8091")
PDF_DIR = os.path.join(ROOT, "output", "pdf")
created = []          # (id, type, employee, wanted stage) for the summary


# --------------------------------------------------------------------------- sign-in
class Role:
    """A workflow account signed in to the console, with its token refreshed as needed."""

    def __init__(self, username):
        self.username = username
        self.acct = prepare(username)
        tok, steps = tokens_for(self.acct, new_password=new_passphrase())
        if tok and self.acct.secret and "otp" not in claims(tok["access_token"]).get("amr", []):
            tok, more = tokens_for(self.acct, prompt="login")      # staff: a code-verified sign-in
            steps += more
        if not tok:
            raise SystemExit(f"Could not sign in as {username}: {steps}")
        self._set(tok)
        print(f"  signed in as {username}")

    def _set(self, tok):
        self.access, self.refresh = tok["access_token"], tok.get("refresh_token")
        self.exp = time.time() + int(tok.get("expires_in", 300))

    @property
    def tok(self):
        if time.time() > self.exp - 45:
            r = http.post(OIDC + "/token", data={"grant_type": "refresh_token", "refresh_token": self.refresh,
                                                 "client_id": "console",
                                                 "client_secret": OIDC_SECRETS["console"]})
            if r.status_code == 200:
                self._set(r.json())
            else:
                tok, _ = tokens_for(self.acct, prompt="login")
                self._set(tok)
        return self.access


def ok(status, data, what, expect=(200, 201)):
    if status not in expect:
        raise SystemExit(f"FAILED: {what} -> HTTP {status}: {data}")
    return data


# --------------------------------------------------------------------------- helpers
def emp_ids(role):
    return {e["username"]: e["id"] for e in ok(*get("/api/employees", role.tok), "list employees")}


def ref(role):
    deps = {d["name"]: d["id"] for d in ok(*get("/api/departments", role.tok), "departments")}
    svcs = {s["name"]: s["id"] for s in ok(*get("/api/services", role.tok), "services")}
    return deps, svcs


def pdf(name):
    with open(os.path.join(PDF_DIR, name), "rb") as f:
        return [{"filename": name, "content_base64": base64.b64encode(f.read()).decode()}]


def raise_request(req, label, stage, **body):
    d = ok(*post("/api/requests", body, req.tok), f"raise {label}")
    created.append((d["id"], body["type"], label, stage))
    print(f"  #{d['id']:<3} {body['type']:<8} {label:<34} -> target stage: {stage}")
    return d["id"]


def approve(role, rid, wait_for_scan=False):
    """Approve; if a document is still being scanned (HTTP 409) wait for the scanner."""
    deadline = time.time() + (240 if wait_for_scan else 0)
    while True:
        status, data = post(f"/api/requests/{rid}/approve", {}, role.tok)
        if status == 200:
            return data
        if status == 409 and time.time() < deadline:
            time.sleep(5)
            continue
        raise SystemExit(f"FAILED: {role.username} approve #{rid} -> HTTP {status}: {data}")


def reject(role, rid, reason):
    return ok(*post(f"/api/requests/{rid}/reject", {"reason": reason}, role.tok), f"reject #{rid}")


def process(it, rid, override=None):
    body = {"override_reason": override} if override else {}
    return ok(*post(f"/api/requests/{rid}/process", body, it.tok), f"IT process #{rid}")


def book_if_needed(req, rid):
    d = ok(*get(f"/api/requests/{rid}", req.tok), f"get #{rid}")
    if d["status"] == "pending_appointment":
        ok(*post(f"/api/requests/{rid}/appointment", {"auto": True}, req.tok), f"book #{rid}")


def provision(euss, rid):
    assets = ok(*get("/api/assets", euss.tok), "assets")
    req = ok(*get(f"/api/requests/{rid}", euss.tok), f"get #{rid}")
    tags = {}
    for kind in ("Laptop", "Account"):
        free = sorted(a["asset_tag"] for a in assets if a["kind"] == kind and a["status"] == "available")
        if free:
            tags[kind] = free[0]
    status, data = post(f"/api/requests/{rid}/provision", {"asset_tags": tags}, euss.tok)
    if status != 200:   # only the kinds still needed are accepted: retry with the one(s) it names
        need = [k for k in tags if k.lower() in str(data).lower()] or list(tags)
        status, data = post(f"/api/requests/{rid}/provision", {"asset_tags": {k: tags[k] for k in need}},
                            euss.tok)
    return ok(status, data, f"EUSS provision #{rid} ({req['type']})")


def full_approval(rid, hr, director, aa, scan=False):
    approve(hr, rid, scan)
    approve(director, rid, scan)
    approve(aa, rid, scan)


def hrms(path, **form):
    # The HRMS page only accepts same-origin form posts (Origin must match its host).
    r = requests.post(HRMS + path, data=form, verify=CA, allow_redirects=True, timeout=30,
                      headers={"Origin": HRMS, "Referer": HRMS + "/"})
    if r.status_code != 200:
        raise SystemExit(f"FAILED: HRMS {path} -> HTTP {r.status_code}")


def in_days(days, hour, minute=0):
    d = dt.datetime.now(SGT) + dt.timedelta(days=days)
    while d.weekday() >= 5:                               # weekdays only
        d += dt.timedelta(days=1)
    return d.replace(hour=hour, minute=minute, second=0, microsecond=0)


# --------------------------------------------------------------------------- main
def finish():
    """--finish: complete a run that stopped after the access-review step (the review
    decision and the HRMS events) without raising any requests again."""
    director, it = Role("director"), Role("itadmin")
    camps = ok(*get("/api/access-reviews", it.tok), "campaigns")
    camps = camps.get("campaigns", camps) if isinstance(camps, dict) else camps
    if camps:
        certify_one(director, max(c["id"] for c in camps))
    hrms_events()


def main():
    if "--finish" in sys.argv:
        return finish()
    print("Signing in as the workflow accounts (this resets their passwords + authenticators)…")
    req = Role("req")
    mine = ok(*get("/api/requests/mine", req.tok), "my requests")
    if mine and "--force" not in sys.argv:
        raise SystemExit(f"`req` already has {len(mine)} request(s): demo data looks seeded. "
                         "Use --force to add another batch anyway.")
    hr, director, aa = Role("hr"), Role("director"), Role("aa")
    it, euss = Role("itadmin"), Role("euss")
    E = emp_ids(req)
    D, S = ref(req)
    laptop, account = S["Personal Laptop"], S["Account"]

    print("\nEUSS desk: one extra appointment slot")
    extra = in_days(3, 17, 30)
    ok(*post("/api/euss/slots", {"starts_at": extra.isoformat(), "minutes": 30, "capacity": 2,
                                 "note": "Extra slot for the onboarding batch"}, euss.tok), "add EUSS slot")
    print(f"  added {extra:%a %d %b %H:%M} SGT (2 places)")

    print("\nRequests raised by req")
    # --- completed (go first: they use department quota) -------------------------------
    r_liam = raise_request(req, "liam -> Engineering", "completed", type="onboard",
                           target_employee_id=E["liam"], department_id=D["Engineering"],
                           justification="New graduate software engineer joining the platform team.")
    r_ivy = raise_request(req, "ivy: Account", "completed", type="grant", target_employee_id=E["ivy"],
                          service_id=account,
                          justification="Needs an Account to use the HR case-management portal.")
    # --- awaiting the EUSS appointment ---------------------------------------------------
    r_henry = raise_request(req, "henry: Personal Laptop (time-bound)", "pending_euss", type="grant",
                            target_employee_id=E["henry"], service_id=laptop,
                            access_expires_at=(dt.datetime.now(SGT) + dt.timedelta(days=60))
                            .strftime("%Y-%m-%dT%H:%M"),
                            justification="Loan laptop for the 2-month regional sales roadshow.")
    r_bob = raise_request(req, "bob: return Personal Laptop", "pending_euss", type="return",
                          target_employee_id=E["bob"], service_id=laptop,
                          justification="Laptop no longer needed: bob moved to a desk-based role.")
    # --- rejected ------------------------------------------------------------------------
    r_grace = raise_request(req, "grace: Personal Laptop", "rejected (Director)", type="grant",
                            target_employee_id=E["grace"], service_id=laptop,
                            justification="Would like a laptop to work from client sites.")
    r_dave = raise_request(req, "dave -> Human Resources", "rejected (HR)", type="onboard",
                           target_employee_id=E["dave"], department_id=D["Human Resources"],
                           justification="Contract HR assistant starting next month.")
    # --- in flight at every approval stage -------------------------------------------------
    r_charlie = raise_request(req, "charlie: Personal Laptop (+ scanned PDF)", "pending_hr", type="grant",
                              target_employee_id=E["charlie"], service_id=laptop,
                              documents=pdf("pdpa-image-only-test-document.pdf"),
                              justification="Field audits at regional offices; approval memo attached.")
    r_erin = raise_request(req, "erin: Account", "pending_director", type="grant",
                           target_employee_id=E["erin"], service_id=account,
                           justification="Needs an Account for the CI/CD release dashboard.")
    r_kate = raise_request(req, "kate -> Sales (+ PDF)", "pending_aa", type="onboard",
                           target_employee_id=E["kate"], department_id=D["Sales"],
                           documents=pdf("pdpa-synthetic-test-document.pdf"),
                           justification="New account executive; signed offer letter attached.")
    r_jack = raise_request(req, "jack: Human Resources -> Engineering", "pending_it", type="shift",
                           target_employee_id=E["jack"], department_id=D["Engineering"],
                           justification="Internal transfer to the HR-systems engineering squad.")

    print("\nMoving requests through the workflow")
    for rid in (r_liam, r_ivy, r_henry, r_bob):
        full_approval(rid, hr, director, aa)
        process(it, rid)
        book_if_needed(req, rid)
    for rid in (r_liam, r_ivy):
        provision(euss, rid)
    approve(hr, r_grace)
    reject(director, r_grace, "Role is office-based; please use the shared hot-desk laptops.")
    reject(hr, r_dave, "Offer letter not yet signed; resubmit once HR has the signed contract.")
    approve(hr, r_erin)
    approve(hr, r_kate, wait_for_scan=True)          # HR is blocked until the PDF has been scanned
    approve(director, r_kate, wait_for_scan=True)
    full_approval(r_jack, hr, director, aa)

    print("\nMandatory Job Rotation")
    ok(*post("/api/rotations/check", {}, hr.tok), "run MJR due check")
    ok(*post(f"/api/rotations/{E['alice']}/decision", {"decision": "rotate", "department_id": D["Sales"],
                                                      "reason": "Due for rotation; Sales needs an engineer."},
             director.tok), "director: rotate alice")
    planned = ok(*get("/api/rotations/planned", req.tok), "planned rotations")
    plan = next((p for p in planned if p.get("employee_id") == E["alice"]), None)
    if plan:
        raise_request(req, "alice: Engineering -> Sales (MJR)", "pending_hr", type="shift",
                      target_employee_id=E["alice"], department_id=D["Sales"],
                      rotation_decision_id=plan["decision"]["id"],
                      justification="Mandatory Job Rotation confirmed by the Director.")
    print("  alice: rotate decision + linked shift request; frank left 'due' for the Director")

    print("\nQuota change proposed by AA (waits for IT)")
    quotas = {x["service_id"]: x for x in ok(*get("/api/quotas", aa.tok), "quotas")}
    new_allowed = quotas[laptop]["allowed"] + 3
    ok(*post("/api/quota-requests", {"service_id": laptop, "new_allowed": new_allowed,
                                     "reason": "Roadshow and onboarding batch need more laptops this quarter."},
             aa.tok), "AA quota request")
    print(f"  Personal Laptop org-wide quota -> {new_allowed} (pending IT)")

    print("\nAccess review campaign (in progress)")
    camp = ok(*post("/api/access-reviews", {"name": "Q4 Sales access recertification",
                                            "department_id": D["Sales"], "reviewer_id": E["director"],
                                            "due_at": (dt.datetime.now(SGT) + dt.timedelta(days=7))
                                            .strftime("%Y-%m-%dT%H:%M")}, it.tok), "create campaign")
    certify_one(director, camp["id"])
    hrms_events()
    summary()


def certify_one(director, campaign_id):
    """Director certifies one row so the campaign shows as 'in progress'."""
    items = ok(*get(f"/api/access-reviews/{campaign_id}", director.tok), "campaign").get("items", [])
    if any(i.get("decision") for i in items):
        print(f"  campaign #{campaign_id}: already has a decision")
        return
    pick = next((i for i in items if i.get("finding_code") == "aligned"), items[0] if items else None)
    if pick:
        ok(*post(f"/api/access-review-items/{pick['id']}/decision",
                 {"decision": "keep", "reason": "Confirmed with the line manager; still needed for the role."},
                 director.tok), "certify one item")
    print(f"  campaign #{campaign_id}: {len(items)} item(s), {1 if pick else 0} certified, rest pending")


def hrms_events():
    print("\nHR system (HRMS) events")
    hrms("/hire", username="nina", full_name="Nina Tan", department="Human Resources",
         reason="New HR business partner")
    hrms("/resign", username="ivy", effective_date=(dt.date.today() + dt.timedelta(days=14)).isoformat(),
         reason="Resigned: relocating overseas")
    print("  hire nina (Human Resources) -> automatic onboard request; ivy resignation scheduled in 14 days")


def summary():
    print("\nDone. Requests created:")
    for rid, rtype, label, stage in created:
        print(f"  #{rid:<3} {rtype:<8} {label:<40} {stage}")
    print("\nNext: docker compose exec analytics python demo_history.py seed")
    print("      python scripts/reset_demo_passwords.py")


if __name__ == "__main__":
    main()
