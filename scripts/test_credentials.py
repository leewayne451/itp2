"""
End-to-end check of credential security against a RUNNING stack.

    python scripts/generate_secrets.py    # once
    docker compose up -d --build          # wait ~30 s for Keycloak
    pip install requests
    python scripts/test_credentials.py    # (from the repo root)

Since CHANGE 004 Keycloak owns passwords, lock-out, sessions and "forgot password";
the backend owns the admin actions and service-to-service authentication. This script
drives Keycloak's pages the way a browser does (see _e2e) and checks:

  generic errors    unknown user and wrong password get the same answer, in about the
                    same time (no username enumeration)
  temporary pw      must be replaced before any session; the policy refuses short,
                    common, username-like and recently used passwords; a long passphrase
                    with spaces works and then signs in directly
  throttling        5 wrong passwords lock the account (even the right one is refused);
                    rapid repeated failures are throttled (Keycloak quick-login check)
  sessions          each sign-in is a separate session; revoking one ends only that one;
                    logout ends the session on the server
  password change   needs the current password; can sign out the other sessions; the old
                    password stops working
  forgot password   same answer for real and unknown users; a made-up reset token fails
  services          /api/service/access refuses anonymous callers and wrong secrets; a
                    service may ask only about itself; a service's token is not an API session
  admin             generated temporary passwords, policy on admin-chosen passwords, no
                    direct password edits, deactivation ends sessions, admin reset signs the
                    user out, forces a change and lifts a lock-out
  audit             the credential events reach the tamper-evident audit trail

Not applicable any more: the per-IP limit (21st login from one IP in a minute -> 429)
belonged to the backend's own /api/login, removed in CHANGE 004; Keycloak throttles
per account instead (checked above). The script prints this as N/A.

Replaces the passwords of the demo accounts it uses and creates + deletes a test account
'zoe' — demo stacks only.
"""
import re
import statistics
import sys
import time
from urllib.parse import urljoin

from _e2e import (BASE, KEYCLOAK, SECRETS, Account, begin_sign_in, check, claims, delete, exchange_code, follow,
                  fresh_token, get, http, kc, kc_require, kc_session_list, kc_sessions, kc_unlock, new_passphrase,
                  oidc_logout, post, prepare, put, refresh, session_for, summary, tokens_for, wait_for_audit,
                  _drive, _form_action, _page_error)

INVALID = "Invalid username or password."


def login_page(b=None, **kw):
    b, r, ctx = begin_sign_in(b=b, **kw)
    return b, follow(b, r, ctx["redirect"]), ctx


def submit_login(b, page, ctx, username, password):
    r = b.post(_form_action(page.text, "kc-form-login"), allow_redirects=False,
               data={"username": username, "password": password, "credentialId": ""})
    return follow(b, r, ctx["redirect"])


def login_error(username, password):
    b, page, ctx = login_page()
    r = submit_login(b, page, ctx, username, password)
    return None if r.is_redirect else _page_error(r.text)


def timed_failure(username):
    b, page, ctx = login_page()
    t0 = time.perf_counter()
    submit_login(b, page, ctx, username, "wrong-password-guess")
    return time.perf_counter() - t0


def main():
    adm_acct, A = session_for("admin")
    it_acct, _ = session_for("itadmin")
    emps = {e["username"]: e for e in get("/api/employees", A)[1]}
    for leftover in ("zoe", "yan"):
        if leftover in emps:
            delete(f"/api/employees/{emps[leftover]['id']}", A)

    # -- generic errors + timing ------------------------------------------------------
    print("-- generic errors")
    e1, e2 = login_error("no-such-user", "whatever"), login_error("erin", "wrong-password")
    kc_unlock("erin")
    check("unknown user and wrong password get the same answer", e1 == e2 == INVALID, (e1, e2))
    ghost = statistics.median(timed_failure("ghost-user") for _ in range(4))
    real = []
    for _ in range(4):
        time.sleep(1.2)                                   # stay under the quick-login throttle
        real.append(timed_failure("frank"))
    real = statistics.median(real)
    kc_unlock("frank")
    check("...and take about the same time (no username enumeration by timing)",
          abs(ghost - real) < 0.5 * max(ghost, real), f"{ghost:.3f}s vs {real:.3f}s")

    # -- temporary password + policy ------------------------------------------------------
    print("-- temporary password and policy")
    bob = prepare("bob")
    b, page, ctx = login_page()
    page = submit_login(b, page, ctx, "bob", bob.password)
    check("temporary password -> must choose a new one (no session yet)",
          'id="kc-passwd-update-form"' in page.text and not page.is_redirect)
    for bad, why in (("short-pw", "too short"), ("password1234", "a common password"),
                     ("bob", "the username"), ("bobby-the-builder-2026", "contains the username")):
        r = b.post(_form_action(page.text, "kc-passwd-update-form"), allow_redirects=False,
                   data={"password-new": bad, "password-confirm": bad})
        page = follow(b, r, ctx["redirect"])
        check(f"policy rejects a password that is {why}", 'id="kc-passwd-update-form"' in page.text,
              _page_error(page.text))
    first_pw = new_passphrase()
    r, steps = _drive(b, page, bob, first_pw, stop_at=ctx["redirect"])
    tok_a = exchange_code(b, r, ctx)
    check("a long passphrase (spaces allowed) is accepted -> session",
          tok_a is not None and " " in first_pw and get("/api/me", tok_a["access_token"])[0] == 200, steps)
    tok, steps = tokens_for(bob)
    check("the new password signs in directly", tok is not None and steps == ["login"], steps)

    # -- sessions ------------------------------------------------------------------------
    print("-- sessions")
    for s in kc_session_list("bob"):                     # start from exactly two known sessions
        if s["id"] not in (claims(tok_a["access_token"]).get("sid"), claims(tok["access_token"]).get("sid")):
            kc("DELETE", f"/sessions/{s['id']}")
    tok_b = tok
    check("each sign-in is listed as its own session", kc_sessions("bob") == 2, kc_sessions("bob"))
    kc("DELETE", f"/sessions/{claims(tok_b['access_token'])['sid']}")
    check("revoking a session ends that one immediately, the other keeps working",
          refresh(tok_b)[0] == 400 and refresh(tok_a)[0] == 200)
    tok_c, _ = tokens_for(bob)
    check("logout ends the session on the server", oidc_logout(tok_c) == 204 and refresh(tok_c)[0] == 400)

    # -- password change ------------------------------------------------------------------
    print("-- password change")
    other, _ = tokens_for(bob)                           # a second device that should be signed out
    b, page, ctx = login_page(prompt="login", kc_action="UPDATE_PASSWORD")
    r = submit_login(b, page, ctx, "bob", "not-the-current-password")
    check("changing the password needs the current password", not r.is_redirect and
          'id="kc-form-login"' in r.text and refresh(other)[0] == 200, _page_error(r.text))
    time.sleep(1.2)
    r = submit_login(b, r, ctx, "bob", bob.password)
    new_pw = new_passphrase()
    r = b.post(_form_action(r.text, "kc-passwd-update-form"), allow_redirects=False,
               data={"password-new": new_pw, "password-confirm": new_pw, "logout-sessions": "on"})
    r = follow(b, r, ctx["redirect"])
    changed = exchange_code(b, r, ctx)
    old_pw, bob.password = bob.password, new_pw
    check("change succeeds and signs out the OTHER sessions",
          changed is not None and refresh(other)[0] == 400)
    check("the old password stops working", login_error("bob", old_pw) == INVALID)
    kc_require("bob", "UPDATE_PASSWORD")               # ask bob to change it again...
    b, page, ctx = login_page()
    page = submit_login(b, page, ctx, "bob", new_pw)
    r = b.post(_form_action(page.text, "kc-passwd-update-form"), allow_redirects=False,
               data={"password-new": old_pw, "password-confirm": old_pw})
    page = follow(b, r, ctx["redirect"])
    check("...and the policy refuses one of the last 3 passwords", 'id="kc-passwd-update-form"' in page.text,
          _page_error(page.text))
    kc_require("bob")                                  # withdraw the change request: bob keeps new_pw
    kc_unlock("bob")

    # -- lock-out and throttling ----------------------------------------------------------
    print("-- lock-out and throttling")
    charlie = prepare("charlie")
    tok, _ = tokens_for(charlie, new_password=new_passphrase())
    for i in range(5):
        time.sleep(1.2)
        login_error("charlie", f"wrong-{i}-guess")
    time.sleep(1.2)
    check("5 wrong passwords lock the account: even the right one is refused",
          login_error("charlie", charlie.password) is not None)
    kc_unlock("charlie")
    login_error("charlie", "quick-wrong-1")
    login_error("charlie", "quick-wrong-2")              # two failures within 1 s
    check("rapid repeated failures are throttled (the right password is refused for a while)",
          login_error("charlie", charlie.password) is not None)
    kc_unlock("charlie")
    print("N/A   per-IP login rate limit (21st attempt -> 429): belonged to the backend /api/login removed in "
          "CHANGE 004; Keycloak throttles per account (checked above)")

    # -- forgot password -------------------------------------------------------------------
    print("-- forgot password")
    answers = []
    for user in ("charlie", "ghost-user"):
        b, page, ctx = login_page()
        link = re.search(r'href="([^"]*login-actions/reset-credentials[^"]*)"', page.text).group(1)
        link = urljoin(page.url, link.replace("&amp;", "&"))         # the page gives a relative link
        r = follow(b, b.get(link, allow_redirects=False))
        r = b.post(_form_action(r.text, "kc-reset-password-form"), allow_redirects=False, data={"username": user})
        r = follow(b, r)
        answers.append(_page_error(r.text))
    check("forgot-password answers identically for real and unknown users",
          answers[0] == answers[1] and answers[0] != "rejected", answers)
    r = http.get(f"{KEYCLOAK}/realms/servicejourney/login-actions/action-token", params={"key": "made-up-token"})
    check("a made-up reset token is rejected", r.status_code == 400 or "invalid" in r.text.lower()
          or "expired" in r.text.lower(), r.status_code)

    # -- service-to-service authentication -----------------------------------------------
    print("-- services")
    r = http.post(BASE + "/api/service/access", json={"service": "service1", "username": "bob"})
    check("/api/service/access refuses anonymous callers", r.status_code == 401)
    if SECRETS.get("service1"):
        c, _ = post("/api/service/access", {"service": "service1", "username": "bob"}, client="service1",
                    secret="not-the-secret")
        check("a wrong client secret is rejected", c == 401)
        c, _ = post("/api/service/access", {"service": "service2", "username": "bob"}, client="service1")
        check("service1 may not ask about service2", c == 403)
        c, d = post("/api/service/access", {"service": "service1", "username": "bob"}, client="service1")
        check("service1 asking about itself gets a decision", c == 200 and "authorized" in d)
        s1, _ = tokens_for(bob, client="service1")
        check("a service sign-in yields the identity but no API session",
              s1 is not None and claims(s1["id_token"]).get("preferred_username") == "bob"
              and get("/api/me", s1["access_token"])[0] == 401)

    # -- administrator actions ------------------------------------------------------------
    print("-- admin")
    A = fresh_token(adm_acct)
    c, d = post("/api/employees", {"username": "zoe", "full_name": "Zoe Newstart", "role": "employee"}, A)
    check("new account gets a generated temporary password (shown once)",
          c == 201 and re.fullmatch(r"(\w{4}-){3}\w{4}", d.get("temporary_password") or ""), d)
    zoe_id, zoe = d.get("id"), Account("zoe", d.get("temporary_password", ""))
    c, _ = post("/api/employees", {"username": "yan", "full_name": "Yan Weak", "password": "yan123"}, A)
    check("an admin-chosen initial password must pass the policy", c == 400)
    c, _ = put(f"/api/employees/{zoe_id}", {"password": "Set directly 12345"}, A)
    check("passwords cannot be edited directly", c == 400)
    tok, _ = tokens_for(zoe, new_password=new_passphrase())
    put(f"/api/employees/{zoe_id}", {"status": "inactive"}, A)
    check("deactivating an account ends its sessions at once",
          get("/api/me", tok["access_token"])[0] == 401 and kc_sessions("zoe") == 0)
    put(f"/api/employees/{zoe_id}", {"status": "active"}, A)
    tok, _ = tokens_for(zoe)
    c, d = post(f"/api/employees/{zoe_id}/password/reset", {}, A)
    check("admin reset: temporary password issued, user signed out",
          c == 200 and d.get("temporary_password") and get("/api/me", tok["access_token"])[0] == 401)
    tok, steps = tokens_for(Account("zoe", d.get("temporary_password", "")), new_password=new_passphrase())
    check("...and the temporary password must be changed at sign-in", steps == ["login", "update_password"], steps)
    for i in range(5):
        time.sleep(1.2)
        login_error("charlie", f"wrong-again-{i}")
    emps = {e["username"]: e for e in get("/api/employees", A)[1]}
    c, d = post(f"/api/employees/{emps['charlie']['id']}/password/reset", {}, A)
    tok, steps = tokens_for(Account("charlie", d.get("temporary_password", "")), new_password=new_passphrase())
    check("an admin reset also unlocks a locked account", c == 200 and tok is not None, steps)
    c, _ = delete(f"/api/employees/{zoe_id}", A)

    # -- audit ------------------------------------------------------------------------------
    need = ["login_failed", "login_blocked", "password_changed", "password_change_failed",
            "password_reset_requested", "password_reset_by_admin", "logout", "account_created",
            "account_updated", "account_deleted"]
    missing = wait_for_audit(fresh_token(it_acct), need)
    check("every credential event is in the audit trail", not missing, missing)
    return summary()


if __name__ == "__main__":
    sys.exit(main())
