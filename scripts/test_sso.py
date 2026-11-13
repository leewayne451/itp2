"""
End-to-end check of Keycloak single sign-on against a RUNNING stack.

    python scripts/generate_secrets.py    # once
    docker compose up -d --build          # wait ~30 s for Keycloak
    pip install requests
    python scripts/test_sso.py            # (from the repo root)

Covers: accounts linked to Keycloak (no credentials left in the app database),
staff sign-in with TOTP (Google Authenticator compatible) and password change,
employees on password only, single sign-on across the console and both services,
single sign-out (back-channel logout), the API's token checks, Keycloak password
policy and brute-force lock-out, every admin action (create, role change,
deactivate, MFA reset, password reset, forced sign-out, delete), suspension on
department shift, the full grant workflow, and Keycloak events in the audit trail.

The "forgot password" e-mail check runs when the mailbox is reachable: set
MAILBOX_URL (e.g. https://localhost:8090 after uncommenting the email port in
docker-compose.yml).

NOTE: replaces the passwords / authenticators of the demo accounts it uses (see
_e2e.prepare) and creates + deletes a test account 'sso-probe'. Demo stacks only.
"""
import os
import re
import html
import sys
import time

from _e2e import (APPS, KEYCLOAK, OIDC, SECRETS, Account, app_sign_in, audit_missing, browser, check, claims,
                  delete, get, kc, kc_credentials, kc_in_staff, kc_sessions, kc_user, new_passphrase, post,
                  prepare, put, session_for, summary, tokens_for, _form_action)

STAFF = ("admin", "requestor", "hr", "director", "aa", "it_admin", "euss")


def main():
    # ------------------------------------------------------------------ set-up
    adm_acct, A = session_for("admin")
    it_acct, IT = session_for("itadmin")
    emps = {e["username"]: e for e in get("/api/employees", A)[1]}
    for leftover in ("sso-probe", "sso-weak"):
        if leftover in emps:
            delete(f"/api/employees/{emps[leftover]['id']}", A)
    emps = {e["username"]: e for e in get("/api/employees", A)[1]}

    # ------------------------------------------------------------------ accounts live in Keycloak
    print("-- accounts")
    login_emps = {u: e for u, e in emps.items() if e["role"] != "system"}
    check("every login-capable employee has a Keycloak account", all(kc_user(u) for u in login_emps),
          [u for u in login_emps if not kc_user(u)])
    check("staff roles are in Keycloak's 'staff' group (TOTP required), employees are not",
          all(kc_in_staff(u) == (e["role"] in STAFF) for u, e in emps.items() if e["status"] == "active"))
    check("the API does not expose Keycloak ids or credentials",
          not any(k in e for e in emps.values() for k in ("keycloak_id", "password_hash", "mfa_secret")))

    # ------------------------------------------------------------------ sign-in + MFA
    print("-- sign-in")
    acct = prepare("hr")
    tok, steps = tokens_for(acct, new_password=new_passphrase())
    check("staff first sign-in: password -> set up authenticator -> new password",
          steps == ["login", "configure_totp", "update_password"], steps)
    tok, steps = tokens_for(acct, prompt="login")
    check("staff sign-in asks for the one-time code; token says pwd+otp",
          steps == ["login", "otp"] and {"pwd", "otp"} <= set(claims(tok["access_token"])["amr"]), steps)
    c, me = get("/api/me", tok["access_token"])
    check("backend accepts the staff token (/api/me)", c == 200 and me.get("username") == "hr" and me.get("mfa"), me)
    alice = prepare("alice")
    tok, steps = tokens_for(alice, new_password=new_passphrase())
    check("employee: temporary password replaced, no authenticator needed",
          steps == ["login", "update_password"] and claims(tok["access_token"])["amr"] == ["pwd"], steps)
    check("backend accepts the employee's password-only token", get("/api/me", tok["access_token"])[0] == 200)
    alice_tok = tok

    # ------------------------------------------------------------------ SSO + single sign-out
    print("-- single sign-on")
    bob = prepare("bob")
    b, r, steps = app_sign_in("console", bob, new_password=new_passphrase())
    r1 = b.get(APPS["service1"] + "/login")
    r2 = b.get(APPS["service2"] + "/login")
    check("one sign-in -> console, Personal Laptop and Account without signing in again",
          r.url.startswith(APPS["console"]) and "localhost:8443" not in r1.url + r2.url, (r.url, r1.url, r2.url))
    b.get(APPS["service2"] + "/logout")
    time.sleep(1)
    s1 = b.get(APPS["service1"] + "/session/status").json()
    c0 = b.get(APPS["console"] + "/", allow_redirects=False)
    check("signing out of one app signs out of all (back-channel logout)",
          s1.get("authorized") is False and c0.status_code == 302, (s1, c0.status_code))

    # ------------------------------------------------------------------ API token checks
    print("-- API token validation")
    t = alice_tok["access_token"]
    head, body, sig = t.split(".")
    check("no token -> 401", get("/api/me")[0] == 401)
    check("tampered token -> 401", get("/api/me", head + "." + body[:-4] + "AAAA." + sig)[0] == 401)
    s1tok, _ = tokens_for(alice, client="service1")
    c, d = get("/api/me", s1tok["access_token"])
    check("a token issued to another app (service1) cannot call the API", c == 401, d)
    check("an ID token is not accepted as an access token", get("/api/me", alice_tok["id_token"])[0] == 401)

    # ------------------------------------------------------------------ password policy + lock-out
    print("-- password policy and lock-out")
    charlie = prepare("charlie")
    for bad, why in (("short-pass", "too short"), ("password1234", "a common password"),
                     ("charlie-is-great-2026", "contains the username")):
        tmp = Account("charlie", charlie.password)
        tok, steps = tokens_for(tmp, new_password=bad)
        check(f"Keycloak refuses a new password that is {why}", tok is None and steps[-1].startswith("ERROR"), steps)
    tok, steps = tokens_for(charlie, new_password=new_passphrase())
    check("a long passphrase is accepted", tok is not None, steps)
    for i in range(5):
        tokens_for(Account("charlie", f"wrong-guess-{i}"))
    tok, steps = tokens_for(charlie)
    check("after 5 wrong passwords even the right one is refused (lock-out)", tok is None, steps)
    c, d = post(f"/api/employees/{emps['charlie']['id']}/password/reset", tok=A)
    charlie = Account("charlie", d.get("temporary_password", ""))
    tok, steps = tokens_for(charlie, new_password=new_passphrase())
    check("admin password reset lifts the lock-out; the temporary password must be replaced",
          c == 200 and tok is not None and steps == ["login", "update_password"], (c, steps))

    # ------------------------------------------------------------------ admin actions
    print("-- admin actions")
    check("admin sees each account's 2FA status (from Keycloak)",
          emps["itadmin"].get("mfa_enabled") is True and emps["alice"].get("mfa_enabled") is False)
    c, d = post("/api/employees", {"username": "sso-weak", "full_name": "Weak Password", "password": "sso-weak",
                                   "role": "employee"}, A)
    check("an admin-chosen password must pass Keycloak's policy; nothing half-created",
          c == 400 and not kc_user("sso-weak") and "sso-weak" not in {e["username"] for e in get("/api/employees", A)[1]}, d)
    c, d = post("/api/employees", {"username": "sso-probe", "full_name": "SSO Probe", "role": "employee"}, A)
    pid = d.get("id")
    probe = Account("sso-probe", d.get("temporary_password", ""))
    check("create account -> Keycloak user with a generated temporary password", c == 201 and kc_user("sso-probe"), d)
    tok, steps = tokens_for(probe, new_password=new_passphrase())
    check("the new user can sign in", tok is not None and get("/api/me", tok["access_token"])[0] == 200, steps)
    put(f"/api/employees/{pid}", {"role": "hr"}, A)
    check("promotion to a staff role: the old token stops working at once",
          get("/api/me", tok["access_token"])[0] == 401 and kc_in_staff("sso-probe"))
    tok, steps = tokens_for(probe)
    check("...and the next sign-in requires setting up an authenticator", "configure_totp" in steps, steps)
    put(f"/api/employees/{pid}", {"role": "employee"}, A)
    tok, steps = tokens_for(probe)
    put(f"/api/employees/{pid}", {"status": "inactive"}, A)
    check("deactivation disables the Keycloak account and refuses the current token",
          kc_user("sso-probe")["enabled"] is False and get("/api/me", tok["access_token"])[0] == 401)
    check("...a deactivated user cannot sign in", tokens_for(probe)[0] is None)
    put(f"/api/employees/{pid}", {"status": "active"}, A)
    c, d = post(f"/api/employees/{emps['hr']['id']}/mfa/reset", tok=A)
    check("MFA reset removes the authenticator in Keycloak", c == 200 and "otp" not in kc_credentials("hr"), d)
    acct.secret = None
    tok, steps = tokens_for(acct)
    check("...and the staff member must enrol a new one at next sign-in", "configure_totp" in steps, steps)
    alice_tok, _ = tokens_for(alice)
    c, _ = post(f"/api/employees/{emps['alice']['id']}/sessions/revoke", tok=A)
    check("forced sign-out ends the Keycloak sessions and refuses the current token at once",
          c == 200 and kc_sessions("alice") == 0 and get("/api/me", alice_tok["access_token"])[0] == 401)
    c, _ = delete(f"/api/employees/{pid}", A)
    check("deleting an account deletes its Keycloak account", c == 200 and not kc_user("sso-probe"))

    # ------------------------------------------------------------------ forgot password (optional)
    mailbox = os.environ.get("MAILBOX_URL")
    if mailbox:
        print("-- forgot password")
        import requests
        mb = requests.Session()
        mb.trust_env = False
        mb.verify = os.path.join(os.path.dirname(__file__), "..", "certs", "ca.crt")
        mb.post(mailbox + "/api/clear")
        prepare("henry")
        fb = browser()
        r = fb.get(OIDC + "/auth", params={"client_id": "console", "response_type": "code", "scope": "openid",
                                           "redirect_uri": APPS["console"] + "/callback",
                                           "code_challenge": "x" * 43, "code_challenge_method": "S256"})
        link = html.unescape(re.search(r'href="([^"]*reset-credentials[^"]*)"', r.text).group(1))
        r = fb.get(link if link.startswith("http") else KEYCLOAK + link)
        fb.post(_form_action(r.text, "kc-reset-password-form"), data={"username": "henry"})
        time.sleep(2)
        mail = [m for m in mb.get(mailbox + "/api/inbox").json() if m["to"].startswith("henry")]
        check("forgot password: Keycloak e-mails a reset link", len(mail) == 1 and "action-token" in mail[0]["body"])
        if mail:
            r = fb.get(re.search(r"(https://\S+action-token\S+)", mail[0]["body"]).group(1).rstrip(".)"))
            if "kc-passwd-update-form" not in r.text:
                m = re.search(r'href="([^"]+)"[^>]*>[^<]*proceed', r.text, re.I)
                r = fb.get(html.unescape(m.group(1)))
            pw = new_passphrase()
            fb.post(_form_action(r.text, "kc-passwd-update-form"), allow_redirects=False,
                    data={"password-new": pw, "password-confirm": pw})
            check("the reset link sets a new password that works", tokens_for(Account("henry", pw))[0] is not None)
    else:
        print("(MAILBOX_URL not set — skipped the forgot-password e-mail check)")

    # ------------------------------------------------------------------ workflow
    print("-- request workflow")
    T = {u: session_for(u)[1] for u in ("req", "director", "aa", "euss")}
    T["hr"] = tokens_for(acct)[0]["access_token"]
    T["itadmin"] = IT
    depts = {d["name"]: d["id"] for d in get("/api/departments", T["req"])[1]}
    c, d = post("/api/requests", {"type": "shift", "target_employee_id": emps["grace"]["id"],
                                  "department_id": depts["Engineering"]}, T["req"])
    check("a department shift suspends the employee in Keycloak", c == 201 and kc_user("grace")["enabled"] is False, d)
    post(f"/api/requests/{d.get('id')}/reject", {"reason": "e2e test"}, T["hr"])
    check("rejecting the shift re-enables the Keycloak account", kc_user("grace")["enabled"] is True)

    laptop = next(x for x in get("/api/services", T["req"])[1] if x["resource_type"] == "laptop")
    rid = None
    for u in ("charlie", "dave", "erin", "frank", "henry", "ivan", "judy", "bob", "alice"):
        if u in emps:
            c, d = post("/api/requests", {"type": "grant", "target_employee_id": emps[u]["id"],
                                          "service_id": laptop["id"], "justification": "e2e test"}, T["req"])
            if c == 201:
                rid, target = d["id"], u
                break
    if not rid:
        print("(no employee without a laptop left — skipped the grant workflow; use a fresh stack)")
    else:
        ok = all(post(f"/api/requests/{rid}/approve", tok=T[u])[0] == 200 for u in ("hr", "director", "aa"))
        pc, pd = post(f"/api/requests/{rid}/process", tok=T["itadmin"])
        ok = ok and pc == 200
        if ok and pd.get("status") == "pending_appointment":   # no EUSS slot was free (CHANGE 011)
            ok = post(f"/api/requests/{rid}/appointment", {"auto": True}, T["req"])[0] == 200
        assets = get("/api/assets", T["euss"])[1]
        free = {k: next((a["asset_tag"] for a in assets if a["kind"] == k and a["status"] == "available"), None)
                for k in ("Laptop", "Account")}
        c, d = post(f"/api/requests/{rid}/provision", {"asset_tags": free}, T["euss"])
        check("grant workflow: request -> HR -> director -> AA -> IT -> appointment -> provisioned",
              ok and c == 200 and d.get("status") == "completed", d)
        if SECRETS.get("service1"):
            c, d = post("/api/service/access", {"service": "service1", "username": target}, client="service1")
            check(f"{target} is now authorised for the Personal Laptop service", d.get("authorized") is True, d)

    # ------------------------------------------------------------------ audit trail
    print("-- audit trail")
    need = ["login_success", "login_failed", "login_blocked", "password_changed", "password_change_failed",
            "mfa_enrolled", "logout", "account_created", "account_deleted", "mfa_reset", "sessions_revoked",
            "password_reset_by_admin", "credentials_migrated_to_keycloak"]
    if mailbox:
        need += ["password_reset_requested", "password_reset_completed"]
    missing = audit_missing(IT, need)
    check("Keycloak sign-in events and admin actions are in the audit trail", not missing, missing)
    _, d = get("/api/audit", IT, params={"action": "login_failed", "actor": "charlie", "page_size": 1})
    e = (d.get("entries") or [{}])[0]
    check("failed sign-ins are attributed to the account, with IP address and app", e.get("ip") and e.get("client"), e)
    _, rep = get("/api/audit/verify", IT)
    check("the audit hash chain verifies", rep.get("ok"), rep.get("problems"))
    return summary()


if __name__ == "__main__":
    sys.exit(main())
