"""
End-to-end check of TOTP two-factor authentication against a RUNNING stack.

    python scripts/generate_secrets.py    # once
    docker compose up -d --build          # wait ~30 s for Keycloak
    pip install requests
    python scripts/test_mfa.py            # (from the repo root)

Since CHANGE 004 two-factor authentication is done by Keycloak (sign-in pages, TOTP
set-up, code checks, lock-out); the backend only insists that STAFF tokens carry a
verified second factor (amr contains "otp"). This script drives Keycloak's pages the
way a browser does (see _e2e._drive) and checks:

  policy       employees sign in with a password only; staff must enrol an
               authenticator before ANY session is issued; the API refuses a staff
               token without a verified code
  enrolment    a wrong code is refused during set-up; set-up issues a session
  sign-in      an enrolled account is asked for a code; a correct code works; the same
               code cannot be replayed; a wrong code is refused; 5 wrong codes lock the
               account (even the right code is then refused)
  admin reset  removes the authenticator in Keycloak, ends the current session at once,
               and the staff member must enrol again
  opt-in       an employee who sets up an authenticator is asked for a code from then on
  staff        a staff member cannot get back to password-only sign-in by removing it
  audit        the MFA events reach the tamper-evident audit trail

Not applicable any more: recovery codes. The pre-Keycloak backend issued 10 recovery
codes; Keycloak's recovery-codes feature is not enabled in this realm, so a lost phone is
handled by the administrator's MFA reset (checked above). The script prints this as N/A.

Takes ~2-4 minutes: Keycloak accepts each code once, so it waits for new 30-second windows.
Replaces the passwords / authenticators of the demo accounts it uses — demo stacks only.
"""
import time

from _e2e import (Account, begin_sign_in, check, claims, exchange_code, follow, fresh_token, get, kc_credentials,
                  kc_remove_otp, kc_require, kc_unlock, new_passphrase, post, prepare, session_for, summary,
                  tokens_for, totp, wait_for_audit, _drive, _form_action, _page_error)


def submit_password(acct, prompt="login"):
    """Open the sign-in page and submit the password. Returns (browser, page, ctx)."""
    b, r, ctx = begin_sign_in(prompt=prompt)
    r = follow(b, r, ctx["redirect"])
    r = b.post(_form_action(r.text, "kc-form-login"), allow_redirects=False,
               data={"username": acct.username, "password": acct.password, "credentialId": ""})
    return b, follow(b, r, ctx["redirect"]), ctx


def submit_code(b, page, ctx, code):
    action = None if page.is_redirect else _form_action(page.text, "kc-otp-login-form")
    if not action:          # no code form (e.g. a locked account is sent back to the password page)
        return page
    r = b.post(action, allow_redirects=False, data={"otp": code})
    return follow(b, r, ctx["redirect"])


def code_now(acct, min_left=10):
    """A code from a 30-second window this account has NOT used yet (Keycloak refuses a
    reused code), with at least `min_left` seconds of the window left so a check that
    submits it twice stays within one window."""
    if 30 - time.time() % 30 < min_left:
        time.sleep(30 - time.time() % 30 + 0.5)
    return totp(acct.secret, acct.username)        # waits for a new window if this one was used


def main():
    adm_acct, _ = session_for("admin")
    it_acct, _ = session_for("itadmin")

    # -- policy -------------------------------------------------------------------
    print("-- policy")
    alice = prepare("alice")
    tok, steps = tokens_for(alice, new_password=new_passphrase())
    check("employee without MFA signs in with a password only (after replacing the temporary one)",
          tok is not None and steps == ["login", "update_password"] and claims(tok["access_token"])["amr"] == ["pwd"],
          steps)

    hr = prepare("hr")
    b, page, ctx = submit_password(hr, prompt=None)
    check("staff role must enrol first: Keycloak shows the authenticator set-up, no session issued",
          'id="kc-totp-settings-form"' in page.text and not page.is_redirect)

    # -- enrolment ------------------------------------------------------------------
    print("-- enrolment")
    raw = page.text.split('name="totpSecret" value="', 1)[1].split('"', 1)[0]
    r = b.post(_form_action(page.text, "kc-totp-settings-form"), allow_redirects=False,
               data={"totp": "000000", "totpSecret": raw, "mode": "manual", "userLabel": "e2e"})
    page = follow(b, r, ctx["redirect"])
    check("a wrong code during enrolment is rejected (still on the set-up page)",
          'id="kc-totp-settings-form"' in page.text, _page_error(page.text))
    r, steps = _drive(b, page, hr, new_passphrase(), stop_at=ctx["redirect"])
    tok = exchange_code(b, r, ctx)
    check("...a correct code completes enrolment and sign-in once the temporary password is replaced",
          tok is not None and steps == ["configure_totp", "update_password"], steps)
    if tok and "otp" not in claims(tok["access_token"]).get("amr", []):
        c, d = get("/api/me", tok["access_token"])
        check("the API refuses a staff token without a verified code (first sign-in after enrolment)",
              c == 401 and d.get("mfa_required"), d)

    # -- sign-in with a code ------------------------------------------------------------
    print("-- sign-in with a code")
    b, page, ctx = submit_password(hr)
    check("an enrolled account is asked for a code", 'id="kc-otp-login-form"' in page.text)
    code = code_now(hr)
    r = submit_code(b, page, ctx, code)
    tok = exchange_code(b, r, ctx)
    check("correct TOTP code -> session whose token records the second factor",
          tok is not None and {"pwd", "otp"} <= set(claims(tok["access_token"])["amr"])
          and get("/api/me", tok["access_token"])[0] == 200)
    hr_tok = tok
    b, page, ctx = submit_password(hr)
    r = submit_code(b, page, ctx, code)
    check("the same code cannot be replayed", not r.is_redirect and 'id="kc-otp-login-form"' in r.text,
          _page_error(r.text))
    kc_unlock("hr")
    time.sleep(1.2)
    b, page, ctx = submit_password(hr)
    good = code_now(hr)
    r = submit_code(b, page, ctx, "123456" if good != "123456" else "654321")
    check("a wrong code is rejected", not r.is_redirect and 'id="kc-otp-login-form"' in r.text)
    for i in range(4):
        time.sleep(1.2)                               # slower than the 1 s quick-login check
        r = submit_code(b, r, ctx, "999999" if good != "999999" else "888888")
    time.sleep(1.2)
    r = submit_code(b, r, ctx, code_now(hr, min_left=5))
    check("5 wrong codes lock the account: even the correct code is then refused", not r.is_redirect,
          _page_error(r.text))
    kc_unlock("hr")

    # -- administrator MFA reset ----------------------------------------------------------
    print("-- administrator reset")
    admin_tok = fresh_token(adm_acct)
    emps = {e["username"]: e for e in get("/api/employees", admin_tok)[1]}
    hr_tok = fresh_token(hr)
    c, _ = post(f"/api/employees/{emps['hr']['id']}/mfa/reset", {}, admin_tok)
    check("administrator can reset a user's MFA (authenticator removed in Keycloak)",
          c == 200 and "otp" not in kc_credentials("hr"))
    check("the reset also ends the user's current session at once", get("/api/me", hr_tok)[0] == 401)
    hr.secret = None
    tok, steps = tokens_for(hr)
    check("after the reset the staff member must enrol again", "configure_totp" in steps, steps)

    # -- employee opt-in ----------------------------------------------------------------
    print("-- employee opt-in")
    kc_require("alice", "CONFIGURE_TOTP")            # what "set up authenticator" on the account page does
    tok, steps = tokens_for(alice)
    check("employee can opt in: set-up happens at the next sign-in", tok is not None and "configure_totp" in steps,
          steps)
    tok, steps = tokens_for(alice, prompt="login")
    check("...and from then on is asked for a code; the API accepts the session",
          tok is not None and steps == ["login", "otp"] and get("/api/me", tok["access_token"])[0] == 200, steps)
    kc_remove_otp("alice")
    tok, steps = tokens_for(alice, prompt="login")
    check("an employee can turn it off again (password only afterwards)",
          tok is not None and steps == ["login"], steps)

    # -- staff cannot drop the second factor -----------------------------------------------
    print("-- staff cannot drop the second factor")
    kc_remove_otp("hr")
    hr.secret = None
    b, page, ctx = submit_password(hr)
    check("a staff member whose authenticator is removed is not let in with a password only: "
          "Keycloak demands a new set-up", 'id="kc-totp-settings-form"' in page.text and not page.is_redirect)

    # -- not applicable ---------------------------------------------------------------------
    print("N/A   recovery codes: replaced by the administrator MFA reset since CHANGE 004 "
          "(Keycloak recovery codes are not enabled in this realm)")

    # -- audit ------------------------------------------------------------------------------
    missing = wait_for_audit(fresh_token(it_acct), ["mfa_enrolled", "mfa_reset", "login_success", "login_failed"])
    check("every MFA event is in the audit trail", not missing, missing)
    return summary()


if __name__ == "__main__":
    raise SystemExit(main())
