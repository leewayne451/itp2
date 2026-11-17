"""Reset every seeded ServiceJourney account to its DEFAULT sign-in state.

Default (services/backend/app/sql/seed.sql + sync.py, CHANGE 004):
  * a TEMPORARY password equal to the username — stored only as the seed's Argon2id
    hash, imported into Keycloak as a hashed credential (Keycloak's password policy
    forbids an admin TYPING a password equal to the username, so the original seed
    hash is re-imported instead, exactly as the first-start migration did);
  * "Update password" required at the next sign-in;
  * no authenticator app (staff are made to set one up at their next sign-in through
    the realm's staff flow); no brute-force lock-out; signed out everywhere.

Accounts with no seed hash (e.g. created later via HRMS or the Admin page) are skipped
and listed — they keep the password an administrator issues to them.

Runs INSIDE the backend container (it uses the backend's own Keycloak service account,
audit trail and token cut-off), from the repo root:

    docker compose exec -T backend python - < scripts/reset_demo_passwords.py
    docker compose exec -T -e ONLY=hr,aa backend python - < scripts/reset_demo_passwords.py
"""
import os
import re
import sys

from app import audit, db, keycloak, sync

ACTOR = {"id": None, "username": "ops:reset-demo-passwords", "role": "system"}
SEED = open("/app/app/sql/seed.sql", encoding="utf-8").read()
HASHES = dict(re.findall(r"\('([a-z0-9._-]+)',\s*'(\$argon2id\$[^']+)'\)", SEED))


def reset(emp):
    kc_id = emp["keycloak_id"]
    cred = sync.argon2_credential(HASHES[emp["username"]])
    sync.revoke_tokens(emp["id"])                    # API refuses older tokens at once
    # current password, authenticator(s) and the password history left by earlier changes
    # (a freshly seeded account has none of the latter two)
    removed = {"password": 0, "otp": 0, "password-history": 0}
    for c in keycloak.credentials(kc_id):
        if c.get("type") in removed:
            keycloak._call("DELETE", f"/users/{kc_id}/credentials/{c['id']}")
            removed[c["type"]] += 1
    rep = keycloak._call("GET", f"/users/{kc_id}").json()
    rep.update(credentials=[cred], requiredActions=["UPDATE_PASSWORD"])
    keycloak._call("PUT", f"/users/{kc_id}", json=rep)
    types = [c.get("type") for c in keycloak.credentials(kc_id)]
    if types != ["password"]:
        raise RuntimeError(f"credentials after reset: {types}")
    keycloak.clear_lockout(kc_id)
    sync.sync_employee(emp["id"], end_sessions=True)   # profile, staff group, sign-out
    audit.record("admin", "password_reset_by_admin", actor=ACTOR, target_type="employee",
                 target_id=emp["id"], target_label=emp["username"],
                 details={"reset_to": "default (seed temporary password, change required at sign-in)",
                          "authenticators_removed": removed["otp"], "signed_out_everywhere": True})
    return removed


def main():
    rows = db.query("SELECT id, username, role, status, keycloak_id FROM employees "
                    "WHERE role <> 'system' ORDER BY id")
    only = [u for u in os.environ.get("ONLY", "").split(",") if u]   # e.g. -e ONLY=hr,aa
    if only:
        rows = [r for r in rows if r["username"] in only]
    done, skipped, failed = [], [], []
    for emp in rows:
        if emp["username"] not in HASHES or not emp["keycloak_id"]:
            skipped.append(emp["username"])
            continue
        try:
            r = reset(emp)
            done.append(emp["username"])
            print(f"  reset {emp['username']:<10} ({emp['role']}, {emp['status']}): removed "
                  f"{r['password']} password / {r['otp']} authenticator / {r['password-history']} history")
        except Exception as e:  # noqa: BLE001 — report every account, keep going
            failed.append(emp["username"])
            print(f"  FAILED {emp['username']}: {e}")
    print(f"\n{len(done)} account(s) reset to default; skipped (no seed default): {skipped or 'none'}; "
          f"failed: {failed or 'none'}")
    return 1 if failed else 0


sys.exit(main())
