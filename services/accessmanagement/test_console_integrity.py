"""CHANGE 012 — console: MBL + casino sections on the Director / HR dashboard and the
officer's declaration form, against a live backend with Keycloak / e-mail stubbed
(see the CHANGE 012 entry in ITP2_changes.txt for how to start it)."""
import re, sys
from datetime import date, timedelta
from app import create_app, token_store, backend_client
from flask import session

token_store.get = lambda handle: {"access": "x"} if handle else None
backend_client._access_token = lambda: "kc-" + session["user"]["username"]
app = create_app()
results = []
def check(name, cond, extra=""):
    results.append(bool(cond)); print(("PASS " if cond else "FAIL ") + name + (f"  [{extra}]" if extra and not cond else ""))

import psycopg2
cur = psycopg2.connect(host="/tmp", port=5433, dbname="accessmgmt", user="accessmgmt", password="x").cursor()
def emp(u):
    cur.execute("SELECT id, username, full_name, email, role FROM employees WHERE username=%s", (u,)); r = cur.fetchone()
    return {"id": r[0], "username": r[1], "full_name": r[2], "email": r[3], "role": r[4]}
def client_as(u):
    c = app.test_client()
    with c.session_transaction() as s:
        s["user"] = emp(u); s["handle"] = "h"
    return c
ORIGIN = {"Origin": "http://localhost"}
text = lambda html: re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html))
def section(h, sid):
    m = re.search(r'<section class="panel" id="%s">(.*?)</section>' % sid, h, re.S)
    return m.group(1) if m else ""

d = client_as("director"); h = d.get("/").get_data(as_text=True)
check("director dashboard still has the MJR panel", "Due for rotation (Mandatory Job Rotation)" in h)
check("MBL + casino sections rendered after MJR", h.find('id="rotation"') < h.find('id="mbl"') < h.find('id="casino"'))
mbl = text(section(h, "mbl"))
check("MBL lists officers with leave to clear, carried-forward and start-by",
      "leave to clear by 31 Dec" in mbl and "Alice Employee" in mbl and "4 c/f" in mbl and "Cannot clear in time" in mbl
      and "forfeit" in mbl and "Start by" in mbl, mbl[:500])
pending_part = mbl.split("All officers")[0]
check("cleared officers are not in the to-clear table (frank, jack)", "Frank Engineer" not in pending_part and "Jack HR" not in pending_part)
check("director is read-only on MBL", "/mbl/leaves" not in h and "/mbl/remind" not in h and "/mbl/entitlements" not in h)
cs = section(h, "casino"); cas = text(cs)
check("casino table has no Post column", "<th>Post</th>" not in cs and "Sensitive post" not in cas and 'name="category"' not in cs)
check("casino section lists rules and statuses", "Late declaration" in cas and "Frequent visitor" in cas
      and "Declare EVERY visit" in cas and "more than 4 times" in cas, cas[:400])
check("director cannot declare", 'action="/casino/declarations"' not in h)

hr = client_as("hr"); h2 = hr.get("/").get_data(as_text=True)
check("HR sees record / cancel / entitlement / remind / on-behalf forms",
      "/mbl/leaves" in h2 and "/mbl/remind" in h2 and "/mbl/entitlements" in h2 and "Record declaration" in h2)
check("HR cannot set sensitive positions", "/sensitive" not in h2)
alice = emp("alice")["id"]
start = date.today() + timedelta(days=7 - date.today().weekday())
r = hr.post("/mbl/leaves", data={"employee_id": alice, "start_date": start.isoformat(),
                                 "end_date": (start + timedelta(days=4)).isoformat(), "note": "Year end"},
            headers=ORIGIN, follow_redirects=True)
t = text(r.get_data(as_text=True))
check("HR records alice's leave -> flash with what is left", "Leave recorded" in t and "still to clear" in t, t[:300])
r = hr.post("/mbl/entitlements", data={"employee_id": emp("jack")["id"], "year": date.today().year,
            "entitled_days": "21", "carried_forward_days": "0"}, headers=ORIGIN, follow_redirects=True)
check("HR sets jack's entitlement", "Leave entitlement updated" in r.get_data(as_text=True))
r = hr.post("/mbl/leaves", data={"employee_id": alice, "start_date": "2026-11-07", "end_date": "2026-11-08"},
            headers=ORIGIN, follow_redirects=True)
check("weekend-only leave -> error flash", "no working days" in r.get_data(as_text=True))
r = hr.post("/mbl/remind", headers=ORIGIN, follow_redirects=True)
check("remind -> flash", "Reminder e-mailed" in r.get_data(as_text=True))
r = hr.post("/casino/declarations", data={"employee_id": emp("erin")["id"], "kind": "visit",
            "visit_date": (date.today() - timedelta(days=10)).isoformat(), "casino": "Marina Bay Sands"}, headers=ORIGIN, follow_redirects=True)
check("HR on-behalf late declaration -> late flash", "it is late" in r.get_data(as_text=True))
check("director cannot record leave (403)", d.post("/mbl/leaves", data={"employee_id": alice}, headers=ORIGIN).status_code == 403)

it = client_as("itadmin"); h6 = it.get("/").get_data(as_text=True)
sp = text(section(h6, "sensitive"))
check("IT Admin dashboard has the Sensitive positions panel", "Sensitive positions" in sp and "Charlie Employee" in sp and "Mark as sensitive" in sp, sp[:300])
r = it.post(f"/integrity/officers/{emp('charlie')['id']}/sensitive", data={"sensitive": "yes"}, headers=ORIGIN, follow_redirects=True)
check("IT Admin marks charlie sensitive", "Marked as a sensitive position" in r.get_data(as_text=True))
check("HR cannot post the switch (403)", hr.post(f"/integrity/officers/{emp('charlie')['id']}/sensitive",
      data={"sensitive": "no"}, headers=ORIGIN).status_code == 403)

c = client_as("charlie"); hc = text(section(c.get("/my-journey").get_data(as_text=True), "casino"))
check("charlie now sees the sensitive rule on My journey", "sensitive position" in hc and "Declare EVERY visit" in hc, hc[:300])
r = c.post("/casino/declarations", data={"kind": "visit", "visit_date": date.today().isoformat(),
                                         "casino": "Resorts World Sentosa"}, headers=ORIGIN, follow_redirects=True)
check("employee declares own visit -> recorded", "Declaration recorded." in r.get_data(as_text=True))
a = client_as("alice"); h4 = text(section(a.get("/my-journey").get_data(as_text=True), "casino"))
check("alice sees her leave to clear on My journey", "Mandatory Block Leave" in h4 and "to clear by 31 December" in h4, h4[:400])

aa = client_as("aa"); h5 = aa.get("/").get_data(as_text=True)
check("AA dashboard unchanged (no MBL / casino panels)", 'id="mbl"' not in h5 and 'id="casino"' not in h5)

print(f"\n{sum(results)}/{len(results)} passed"); sys.exit(0 if all(results) else 1)
