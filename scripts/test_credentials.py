"""
End-to-end check of credential security against a RUNNING stack: Argon2id
passwords, the password policy, forced change of temporary passwords, lockout,
rate limiting, sessions (revoke / sign out everywhere / logout), password change,
forgot-password, admin resets and service-to-service authentication.

    python scripts/generate_secrets.py    # once
    docker compose up -d --build
    pip install requests pyotp
    python scripts/test_credentials.py    # (from the repo root)

Run it on a FRESH stack (`docker compose down -v` first). It replaces the
temporary passwords of a few demo accounts (printed at the end) and creates a
test account 'zoe'; run `docker compose down -v` afterwards to reset.
"""
import re
import statistics
import sys
import time

from _e2e import (check, get, post, put, new_ip, finish_password_change, summary, http, BASE, SECRETS,
                  staff_session, audit_missing, reset_mfa)

PW = {"bob": "Pebble orchard violin 19", "charlie": "Saffron canoe lattice 73",
      "admin": "Granite wombat fiddle 88", "itadmin": "Bluebird canyon river 7"}
INVALID = "Invalid username or password."


def sign_in(user, password):
    _, d = post("/api/login", {"username": user, "password": password})
    return d


def main():
    # -- generic errors + timing -------------------------------------------------
    new_ip()
    c1, d1 = post("/api/login", {"username": "no-such-user", "password": "whatever"})
    c2, d2 = post("/api/login", {"username": "erin", "password": "wrong-password"})
    check("unknown user and wrong password get the same answer", c1 == c2 == 401 and d1 == d2 == {"error": INVALID})

    def timed(u):
        t0 = time.perf_counter()
        post("/api/login", {"username": u, "password": "wrong-password"})
        return time.perf_counter() - t0
    new_ip()
    ghost = statistics.median(timed("ghost-user") for _ in range(4))
    new_ip()
    real = statistics.median(timed("frank") for _ in range(4))
    check("…and take about the same time (no username enumeration by timing)",
          abs(ghost - real) < 0.5 * max(ghost, real), f"{ghost:.3f}s vs {real:.3f}s")

    # -- temporary password + policy -------------------------------------------------
    new_ip()
    d = sign_in("bob", "bob")
    check("temporary password -> must choose a new one (no session yet)",
          d.get("password_change_required") and "token" not in d)
    change = d["change_token"]
    check("the change token is not a session", get("/api/mfa/status", change)[0] == 401)
    for bad, why in [("short-pass", "too short"), ("password1234", "common"), ("Password2024!", "common base"),
                     ("bob-employee-123", "contains name"), ("aaaaaaaaaaaaaa", "repetitive"),
                     ("abcdefghijklmn", "a sequence")]:
        c, _ = post("/api/login/password", {"change_token": change, "new_password": bad})
        check(f"policy rejects a password that is {why}", c == 400)
    c, d = post("/api/login/password", {"change_token": change, "new_password": PW["bob"]})
    check("a long passphrase (spaces allowed) is accepted -> session", c == 200 and "token" in d)
    bob = d["token"]
    check("the new password signs in directly", "token" in sign_in("bob", PW["bob"]))

    # -- sessions -------------------------------------------------------------------
    post("/api/sessions/revoke-others", {}, bob)             # start from a single session
    bob2 = sign_in("bob", PW["bob"])["token"]
    _, sess = get("/api/sessions", bob)
    check("'where you're signed in' lists both sessions", len(sess) == 2 and sum(s["current"] for s in sess) == 1)
    other = next(s["id"] for s in sess if not s["current"])
    post(f"/api/sessions/{other}/revoke", {}, bob)
    check("revoking a session ends it immediately",
          get("/api/sessions", bob2)[0] == 401 and get("/api/sessions", bob)[0] == 200)
    bob3 = sign_in("bob", PW["bob"])["token"]
    post("/api/sessions/revoke-others", {}, bob)
    check("'sign out everywhere else' ends the others, keeps this one",
          get("/api/sessions", bob3)[0] == 401 and get("/api/sessions", bob)[0] == 200)
    post("/api/logout", {}, bob)
    check("logout ends the session on the server", get("/api/sessions", bob)[0] == 401)

    # -- password change ------------------------------------------------------------
    new_ip()
    b1, b2 = sign_in("bob", PW["bob"])["token"], sign_in("bob", PW["bob"])["token"]
    c, _ = post("/api/password/change", {"current_password": "not-it", "new_password": "Brand new phrase 404"}, b1)
    check("changing needs the current password", c == 400 and get("/api/sessions", b1)[0] == 200)
    new_pw = "Copper lantern meadow 7"
    c, d = post("/api/password/change", {"current_password": PW["bob"], "new_password": new_pw}, b1)
    check("change succeeds and signs out the OTHER sessions",
          c == 200 and d.get("other_sessions_ended") == 1 and get("/api/sessions", b2)[0] == 401)
    check("the old password stops working", sign_in("bob", PW["bob"]).get("error") == INVALID)
    PW["bob"] = new_pw

    # -- lockout ------------------------------------------------------------------------
    new_ip()
    d = finish_password_change(sign_in("charlie", "charlie"), PW["charlie"])
    for i in range(5):
        d = sign_in("charlie", f"wrong-{i}-guess")
    check("5 wrong passwords lock the account", d.get("locked"))
    check("even the right password is refused while locked", sign_in("charlie", PW["charlie"]).get("locked"))

    # -- forgot password -------------------------------------------------------------------
    new_ip()
    _, f1 = post("/api/password/forgot", {"username": "charlie"})
    _, f2 = post("/api/password/forgot", {"username": "ghost-user"})
    check("forgot-password answers identically for real and unknown users", f1 == f2 and f1.get("ok"))
    _, d = post("/api/password/reset", {"token": "made-up-token", "new_password": "Anything long enough 1"})
    check("a made-up reset token is rejected", "invalid" in d.get("error", ""))

    # -- rate limiting -------------------------------------------------------------------
    new_ip()
    # raw calls: the helpers retry on 429, here we want to see it
    codes = []
    for _ in range(21):
        r = http.post(BASE + "/api/login", json={"username": "ghost", "password": "x"},
                      headers={"X-Client-Id": "console", "X-Client-Secret": SECRETS["console"],
                               "X-Forwarded-For": "203.0.113.77"})
        codes.append(r.status_code)
    check("the 21st login attempt in a minute from one IP gets 429", codes[:20] == [401] * 20 and codes[20] == 429,
          codes)

    # -- service-to-service authentication -----------------------------------------------
    r = http.post(BASE + "/api/service/access", json={"service": "service1", "username": "bob"})
    check("/api/service/access refuses anonymous callers", r.status_code == 401)
    if SECRETS.get("service1"):
        c, _ = post("/api/service/access", {"service": "service1", "username": "bob"},
                    client="service1", secret="x" * 64)
        check("a wrong client secret is rejected", c == 401)
        c, _ = post("/api/service/access", {"service": "service2", "username": "bob"}, client="service1")
        check("service1 may not ask about service2", c == 403)
        c, d = post("/api/service/access", {"service": "service1", "username": "bob"}, client="service1")
        check("service1 asking about itself gets a decision", c == 200 and "authorized" in d)
        c, d = post("/api/login", {"username": "bob", "password": PW["bob"]}, client="service1")
        check("a service login yields the identity but no console session", c == 200 and d.get("user")
              and "token" not in d)

    # -- administrator ---------------------------------------------------------------------
    new_ip()
    adm = staff_session("admin")
    c, d = post("/api/employees", {"username": "zoe", "full_name": "Zoe Newstart", "role": "employee"}, adm)
    check("new account gets a generated temporary password (shown once)",
          c == 201 and re.fullmatch(r"(\w{4}-){3}\w{4}", d.get("temporary_password") or ""))
    zoe_id, temp = d.get("id"), d.get("temporary_password")
    c, _ = post("/api/employees", {"username": "yan", "full_name": "Yan Weak", "password": "yan123"}, adm)
    check("an admin-chosen initial password must pass the policy", c == 400)
    c, _ = put(f"/api/employees/{zoe_id}", {"password": "Set directly 12345"}, adm)
    check("passwords cannot be edited directly", c == 400)
    zoe = finish_password_change(sign_in("zoe", temp), "Zebra tulip canyon 64")["token"]
    put(f"/api/employees/{zoe_id}", {"status": "inactive"}, adm)
    check("deactivating an account ends its sessions at once", get("/api/sessions", zoe)[0] == 401)
    put(f"/api/employees/{zoe_id}", {"status": "active"}, adm)
    zoe = sign_in("zoe", "Zebra tulip canyon 64")["token"]
    c, d = post(f"/api/employees/{zoe_id}/password/reset", {}, adm)
    check("admin reset: temporary password issued, user signed out",
          c == 200 and d.get("temporary_password") and get("/api/sessions", zoe)[0] == 401)
    check("…and the temporary password must be changed at sign-in",
          sign_in("zoe", d["temporary_password"]).get("password_change_required"))
    emps = {e["username"]: e for e in get("/api/employees", adm)[1]}
    c, d = post(f"/api/employees/{emps['charlie']['id']}/password/reset", {}, adm)
    check("an admin reset also unlocks a locked account",
          c == 200 and sign_in("charlie", d["temporary_password"]).get("password_change_required"))

    it_tok = staff_session("itadmin")          # the audit trail is readable by IT Admin only
    need = {"login_failed", "login_blocked", "account_locked", "password_change_required", "password_changed",
            "password_reset_requested", "password_reset_by_admin", "session_revoked", "sessions_revoked",
            "logout", "account_created", "account_updated"}
    missing = audit_missing(it_tok, need)
    check("every credential event is in the audit log", not missing, missing)
    reset_mfa(adm, "itadmin", "admin")

    print("\nPasswords now set by this test:", ", ".join(f"{u} = '{p}'" for u, p in PW.items()),
          "(charlie: temporary password from the admin reset)")
    return summary()


if __name__ == "__main__":
    sys.exit(main())
