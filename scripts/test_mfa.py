"""
End-to-end check of TOTP two-factor authentication against a RUNNING stack.

    python scripts/generate_secrets.py    # once
    docker compose up -d --build
    pip install requests pyotp
    python scripts/test_mfa.py            # (from the repo root)

Run it on a FRESH stack (`docker compose down -v` first). It signs in as real demo
accounts, so it REPLACES their temporary passwords (printed at the end) and enrols
/ resets MFA. Afterwards run `docker compose down -v` to get the demo state back.
Takes ~1-2 minutes because it waits for new 30-second TOTP windows.
"""
import sys

import pyotp

from _e2e import (check, get, post, new_ip, next_window, finish_password_change, summary, staff_session,
                  audit_missing, reset_mfa)

PW = {"hr": "Lilac tractor anthem 51", "admin": "Granite wombat fiddle 88", "alice": "Violet kettle orbit 42",
      "itadmin": "Bluebird canyon river 7"}


def main():
    # -- policy ---------------------------------------------------------------
    new_ip()
    c, d = post("/api/login", {"username": "alice", "password": "alice"})
    d = finish_password_change(d, PW["alice"])
    check("employee without MFA signs in with password only (after replacing the temporary one)",
          "token" in d and not d.get("mfa_required"))
    alice_tok = d.get("token")

    c, d = post("/api/login", {"username": "hr", "password": "hr"})
    check("staff role must enrol first (no session handed out)",
          d.get("mfa_enrollment_required") and "token" not in d)
    challenge = d["mfa_token"]
    c, _ = get("/api/requests/queue", challenge)
    check("the MFA challenge is not usable as an API session", c == 401)

    # -- enrolment ------------------------------------------------------------
    c, st = post("/api/login/mfa/enroll/start", {"mfa_token": challenge})
    check("enrolment returns an otpauth:// URI", c == 200 and st["otpauth_uri"].startswith("otpauth://totp/"))
    totp = pyotp.TOTP(st["secret"])
    c, _ = post("/api/login/mfa/enroll/confirm", {"mfa_token": challenge, "code": "000000"})
    check("wrong code during enrolment is rejected", c == 400)
    c, d = post("/api/login/mfa/enroll/confirm", {"mfa_token": challenge, "code": totp.now()})
    check("enrolment returns 10 recovery codes", c == 200 and len(d.get("recovery_codes", [])) == 10)
    recovery = d["recovery_codes"]
    d = finish_password_change(d, PW["hr"])
    check("...and completes sign-in once the temporary password is replaced", "token" in d)
    hr_tok = d["token"]

    # -- sign-in with the second factor ----------------------------------------
    next_window()
    new_ip()
    _, d = post("/api/login", {"username": "hr", "password": PW["hr"]})
    check("enrolled account is asked for a code", d.get("mfa_required") and "token" not in d)
    challenge, code = d["mfa_token"], totp.now()
    c, d = post("/api/login/mfa", {"mfa_token": challenge, "code": code})
    check("correct TOTP code -> session", c == 200 and d.get("method") == "totp" and "token" in d)
    c, _ = post("/api/login/mfa", {"mfa_token": challenge, "code": code})
    check("the same code cannot be replayed", c == 401)
    c, _ = post("/api/login/mfa", {"mfa_token": challenge, "code": "123456" if code != "123456" else "654321"})
    check("wrong code is rejected", c == 401)
    c, d = post("/api/login/mfa", {"mfa_token": challenge, "code": recovery[0]})
    check("recovery code works once", c == 200 and d.get("recovery_codes_remaining") == 9)
    c, _ = post("/api/login/mfa", {"mfa_token": challenge, "code": recovery[0]})
    check("...and only once", c == 401)

    # -- brute force -------------------------------------------------------------
    new_ip()
    for _ in range(5):
        post("/api/login/mfa", {"mfa_token": challenge, "code": "999999" if totp.now() != "999999" else "888888"})
    c, d = post("/api/login/mfa", {"mfa_token": challenge, "code": totp.now()})
    check("5 wrong codes lock the second factor", c == 401 and "Too many" in d.get("error", ""))

    # -- admin reset ---------------------------------------------------------------
    new_ip()
    _, d = post("/api/login", {"username": "admin", "password": "admin"})
    _, st = post("/api/login/mfa/enroll/start", {"mfa_token": d["mfa_token"]})
    _, d = post("/api/login/mfa/enroll/confirm", {"mfa_token": d["mfa_token"], "code": pyotp.TOTP(st["secret"]).now()})
    admin_tok = finish_password_change(d, PW["admin"])["token"]
    emps = {e["username"]: e for e in get("/api/employees", admin_tok)[1]}
    c, _ = post(f"/api/employees/{emps['hr']['id']}/mfa/reset", {}, admin_tok)
    check("administrator can reset a user's MFA", c == 200)
    c, _ = get("/api/mfa/status", hr_tok)
    check("the reset also signs the user out", c == 401)
    _, d = post("/api/login", {"username": "hr", "password": PW["hr"]})
    check("after the reset the user enrols again", d.get("mfa_enrollment_required"))

    # -- opt-in for an employee ------------------------------------------------------
    new_ip()
    _, d = post("/api/login", {"username": "alice", "password": PW["alice"]})
    other_tok = d["token"]
    c, st = post("/api/mfa/setup", {}, alice_tok)
    a_totp = pyotp.TOTP(st["secret"])
    c, d = post("/api/mfa/confirm", {"code": a_totp.now()}, alice_tok)
    check("employee can opt in", c == 200 and len(d.get("recovery_codes", [])) == 10)
    check("this session is upgraded and keeps working", get("/api/mfa/status", alice_tok)[0] == 200)
    check("her other password-only session is ended", get("/api/mfa/status", other_tok)[0] == 401)
    next_window()
    c, _ = post("/api/mfa/disable", {"code": a_totp.now()}, alice_tok)
    check("employee can turn it off again with a code", c == 200)
    c, _ = post("/api/mfa/disable", {"code": "000000"}, admin_tok)
    check("a role that requires MFA cannot turn it off", c == 403)

    # -- audit ---------------------------------------------------------------------
    it_tok = staff_session("itadmin")          # the audit trail is readable by IT Admin only
    need = {"password_ok", "mfa_enrolled", "mfa_success", "mfa_failed", "mfa_locked",
            "recovery_code_used", "mfa_reset", "mfa_disabled", "login_success"}
    missing = audit_missing(it_tok, need)
    check("every MFA event is in the audit log", not missing, missing)

    # -- clean up MFA (passwords stay changed: `docker compose down -v` resets them)
    reset_mfa(admin_tok, "itadmin", "admin")
    print("\nPasswords now set by this test:", ", ".join(f"{u} = '{p}'" for u, p in PW.items()))
    return summary()


if __name__ == "__main__":
    sys.exit(main())
