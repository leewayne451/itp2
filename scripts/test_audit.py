"""
End-to-end check of the audit trail against a RUNNING stack.

    python scripts/generate_secrets.py    # once
    docker compose up -d --build
    pip install requests
    python scripts/test_audit.py          # (from the repo root)

Checks: the events from remarks.txt are recorded (sign-ins, failed sign-ins —
now reported by Keycloak and pulled into the trail —
admin edits to users and roles, IT department-quota changes, access checks),
only IT Admin can read/download the trail, there is no way to change it through
the API, the hash chain verifies, and a JSONL download can be re-verified
independently with AUDIT_HMAC_KEY from .env. If the `docker` CLI is available it
also proves the DATABASE refuses UPDATE/DELETE/TRUNCATE on audit_log.

It creates and deletes a test account 'audit-probe' and replaces the passwords /
authenticators of the demo accounts it signs in as (see _e2e.prepare). Demo stacks only.
"""
import csv
import hashlib
import hmac
import io
import json
import shutil
import subprocess
import sys

from _e2e import (check, get, post, put, summary, http, BASE, ENV, SECRETS, ROOT, headers,
                  session_for, audit_missing, tokens_for, Account)


def _sql(user, statement):
    """Run SQL inside the postgres container as `user`; returns (returncode, output)."""
    cmd = ["docker", "compose", "exec", "-T", "postgres", "psql", "-U", user, "-d", "accessmgmt",
           "-v", "ON_ERROR_STOP=1", "-c", statement]
    p = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, timeout=60)
    return p.returncode, (p.stdout + p.stderr)


def main():
    it = session_for("itadmin")[1]
    adm = session_for("admin")[1]

    # -- who may read it ---------------------------------------------------------------
    check("IT Admin can read the audit trail", get("/api/audit", it)[0] == 200)
    check("the administrator cannot (separation of duties)", get("/api/audit", adm)[0] == 403)
    for method in ("PUT", "PATCH", "DELETE"):
        r = http.request(method, BASE + "/api/audit", headers=headers(it), json={})
        check(f"there is no {method} on the audit API", r.status_code == 405)

    # -- the events listed in remarks.txt ----------------------------------------------------
    tokens_for(Account("bob", "wrong-password"))       # a failed sign-in at Keycloak
    old = next((e for e in get("/api/employees", adm)[1] if e["username"] == "audit-probe"), None)
    if old:
        http.delete(BASE + f"/api/employees/{old['id']}", headers=headers(adm))
    c, d = post("/api/employees", {"username": "audit-probe", "full_name": "Audit Probe", "role": "employee"}, adm)
    pid = d.get("id")
    put(f"/api/employees/{pid}", {"role": "requestor"}, adm)
    http.delete(BASE + f"/api/employees/{pid}", headers=headers(adm))
    depts = {x["name"]: x for x in get("/api/departments", it)[1]}
    svcs = {x["resource_type"]: x for x in get("/api/services", it)[1]}
    q = get("/api/department-quotas", it)[1]
    sales = next(x for x in q if x["department_name"] == "Sales")
    lap = next(r for r in sales["resources"] if r["resource_type"] == "laptop")
    put(f"/api/departments/{depts['Sales']['id']}/quotas", {"quotas": {str(svcs['laptop']['id']): lap["allowed"] + 1}}, it)
    put(f"/api/departments/{depts['Sales']['id']}/quotas", {"quotas": {str(svcs['laptop']['id']): lap["allowed"]}}, it)
    if SECRETS.get("service1"):
        post("/api/service/access", {"service": "service1", "username": "charlie", "context": "login"},
             client="service1")
    get("/api/quotas", adm)   # allowed; and a denied call:
    get("/api/audit/verify", adm)
    need = ["login_success", "login_failed", "account_created", "account_updated", "account_deleted",
            "department_quota_changed", "permission_denied"] + (["service_access_check"] if SECRETS.get("service1") else [])
    missing = audit_missing(it, need)
    check("sign-ins, failed sign-ins, user/role edits, quota changes and access checks are all recorded",
          not missing, missing)
    _, d = get("/api/audit?target=audit-probe&page_size=50", it)
    check("a deleted account's history stays in the trail", d.get("total", 0) >= 3)
    _, d = get("/api/audit?action=account_updated&target=audit-probe", it)
    check("role edit recorded as old -> new",
          any(e["details"].get("changes", {}).get("role") == {"from": "employee", "to": "requestor"}
              for e in d.get("entries", [])))

    # -- integrity -----------------------------------------------------------------------
    _, rep = get("/api/audit/verify", it)
    check("the hash chain verifies", rep.get("ok") and rep.get("entries", 0) > 0, rep.get("problems"))

    # -- download ------------------------------------------------------------------------
    r = http.get(BASE + "/api/audit/export", params={"format": "csv"}, headers=headers(it))
    rows = list(csv.reader(io.StringIO(r.text)))
    check("CSV download with matching SHA-256 header", r.status_code == 200 and rows[0][0] == "seq"
          and r.headers.get("X-Audit-SHA256") == hashlib.sha256(r.content).hexdigest())
    r = http.get(BASE + "/api/audit/export", params={"format": "jsonl"}, headers=headers(it))
    key = ENV.get("AUDIT_HMAC_KEY", "").encode()
    prev, ok, lines = "0" * 64, bool(key), r.text.splitlines()
    for line in lines:
        rec = json.loads(line)
        payload = json.dumps(rec["payload"], sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        ok = ok and rec["prev_hash"] == prev and hmac.compare_digest(
            hmac.new(key, (prev + payload).encode(), hashlib.sha256).hexdigest(), rec["hash"])
        prev = rec["hash"]
    check("JSONL download re-verifies independently with AUDIT_HMAC_KEY", ok and len(lines) > 0)
    check("the download itself is audited", not audit_missing(it, ["exported"]))
    check("the admin cannot download it",
          http.get(BASE + "/api/audit/export", params={"format": "csv"}, headers=headers(adm)).status_code == 403)

    # -- the database itself refuses changes (needs the docker CLI) -------------------------
    stack_up = False
    if shutil.which("docker"):
        try:
            p = subprocess.run(["docker", "compose", "ps", "-q", "postgres"], cwd=ROOT, capture_output=True,
                               text=True, timeout=30)
            stack_up = p.returncode == 0 and p.stdout.strip() != ""
        except (OSError, subprocess.SubprocessError):
            stack_up = False
    if stack_up:
        try:
            for stmt in ("UPDATE audit_log SET action='x' WHERE seq=1", "DELETE FROM audit_log",
                         "TRUNCATE audit_log"):
                rc, out = _sql("sj_app", stmt)
                check(f"runtime DB role refused: {stmt.split()[0]}", rc != 0 and "permission denied" in out, out)
                rc, out = _sql("accessmgmt", stmt)
                check(f"even the table owner refused: {stmt.split()[0]}", rc != 0 and "append-only" in out, out)
        except (OSError, subprocess.SubprocessError) as e:
            print(f"(skipped database checks: {e})")
    else:
        print("(docker CLI / compose stack not available here — skipped the direct database checks)")

    return summary()


if __name__ == "__main__":
    sys.exit(main())
