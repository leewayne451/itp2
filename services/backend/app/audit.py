"""
Tamper-evident, append-only audit trail.

Every security-relevant event is written by record() as ONE row of `audit_log`
plus ONE line of a JSON Lines file on the backend's own volume.

Integrity, in layers:
  1. No API can change or delete an entry — there is simply no such endpoint.
  2. The API runs as a database role that holds only SELECT + INSERT on audit_log
     (db.provision_runtime_role), so even a bug / SQL injection cannot modify it.
  3. A trigger rejects UPDATE, DELETE and TRUNCATE for every role, the owner included.
  4. Each row is chained: hash = HMAC-SHA256(key, prev_hash || payload). Changing,
     removing or re-ordering any row — even by someone with direct database
     superuser access — breaks the chain, and without AUDIT_HMAC_KEY (kept in .env,
     not in the database) they cannot forge a new valid chain.
  5. The JSON Lines copy is compared with the table by verify(), which catches
     rows cut off the END of the table (the one edit a hash chain alone can't see).
"""
import hashlib
import hmac
import json
import os
import threading
from datetime import datetime, timezone

from flask import g, has_request_context, request

from . import db
from .config import AUDIT_HMAC_KEY, AUDIT_LOG_DIR

GENESIS = "0" * 64
CATEGORIES = ("auth", "admin", "config", "quota", "access", "workflow", "asset", "audit")
_LOCK_KEY = 0x5A_A0D1_7  # advisory-lock id that serialises chain appends
_write_lock = threading.Lock()
_KEY = AUDIT_HMAC_KEY.encode()


def _canonical(obj):
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def _mac(prev_hash, payload):
    return hmac.new(_KEY, (prev_hash + payload).encode("utf-8"), hashlib.sha256).hexdigest()


def _request_actor():
    if has_request_context() and g.get("user"):
        u = g.user
        return {"id": u["id"], "username": u["username"], "role": u["role"]}
    return None


def _request_context():
    if not has_request_context():
        return {"ip": None, "client": None}
    from .clients import client_ip
    return {"ip": client_ip() or None, "client": g.get("client")}


def record(category, action, *, outcome="success", actor=None, target_type=None, target_id=None,
           target_label=None, details=None, ts=None, ip=None, client=None):
    """Append one audit entry. `actor` defaults to the signed-in user of the
    current request. Never raises into the caller: an audit failure is reported
    loudly on stderr (and shows up as a gap in verify()) instead of breaking the
    business action that has already happened."""
    assert category in CATEGORIES, category
    try:
        actor = actor or _request_actor() or {}
        ctx = _request_context()
        ts = (ts or datetime.now(timezone.utc)).astimezone(timezone.utc)
        with _write_lock:
            conn = db.get_connection()
            try:
                with conn.cursor() as cur:
                    cur.execute("SELECT pg_advisory_xact_lock(%s)", (_LOCK_KEY,))
                    cur.execute("SELECT seq, hash FROM audit_log ORDER BY seq DESC LIMIT 1")
                    last = cur.fetchone()
                    seq, prev = (last[0] + 1, last[1]) if last else (1, GENESIS)
                    body = {
                        "seq": seq, "ts": ts.isoformat(timespec="microseconds"),
                        "category": category, "action": action, "outcome": outcome,
                        "actor": {"id": actor.get("id"), "username": actor.get("username"),
                                  "role": actor.get("role")},
                        "target": {"type": target_type, "id": None if target_id is None else str(target_id),
                                   "label": target_label},
                        "ip": ip if ip is not None else ctx["ip"],
                        "client": client if client is not None else ctx["client"],
                        "details": details or {},
                    }
                    payload = _canonical(body)
                    digest = _mac(prev, payload)
                    cur.execute(
                        """INSERT INTO audit_log (seq, ts, category, action, outcome, actor_id, actor_username,
                                                  actor_role, target_type, target_id, target_label, ip, client,
                                                  payload, prev_hash, hash)
                           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                        (seq, ts, category, action, outcome, body["actor"]["id"], body["actor"]["username"],
                         body["actor"]["role"], target_type, body["target"]["id"], target_label,
                         body["ip"], body["client"], payload, prev, digest))
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()
            _append_file(seq, prev, digest, payload, ts)
        return seq
    except Exception as e:  # noqa: BLE001
        print(f"[audit] FAILED to record {category}.{action}: {type(e).__name__}: {e}", flush=True)
        return None


def _append_file(seq, prev, digest, payload, ts):
    try:
        os.makedirs(AUDIT_LOG_DIR, exist_ok=True)
        path = os.path.join(AUDIT_LOG_DIR, f"audit-{ts:%Y-%m-%d}.jsonl")
        line = _canonical({"seq": seq, "prev_hash": prev, "hash": digest, "payload": json.loads(payload)})
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o640)   # append-only open
        try:
            os.write(fd, (line + "\n").encode("utf-8"))
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError as e:
        print(f"[audit] could not write file copy for seq {seq}: {e}", flush=True)


# --------------------------------------------------------------------------- #
# Integrity verification
# --------------------------------------------------------------------------- #
_COLUMN_CHECKS = (("category", lambda b: b["category"]), ("action", lambda b: b["action"]),
                  ("outcome", lambda b: b["outcome"]), ("actor_username", lambda b: b["actor"]["username"]),
                  ("target_id", lambda b: b["target"]["id"]))


def _file_index():
    """seq -> hash from the JSON Lines copy (and any unreadable lines)."""
    index, bad = {}, 0
    if not os.path.isdir(AUDIT_LOG_DIR):
        return index, bad
    for name in sorted(os.listdir(AUDIT_LOG_DIR)):
        if not (name.startswith("audit-") and name.endswith(".jsonl")):
            continue
        with open(os.path.join(AUDIT_LOG_DIR, name), encoding="utf-8") as f:
            for line in f:
                try:
                    rec = json.loads(line)
                    index[int(rec["seq"])] = rec["hash"]
                except (ValueError, KeyError, TypeError):
                    bad += 1
    return index, bad


def verify():
    """Walk the whole chain. Returns a report dict; ok is False on any tampering."""
    problems = []
    conn = db.get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("""SELECT seq, payload, prev_hash, hash, category, action, outcome, actor_username, target_id
                           FROM audit_log ORDER BY seq""")
            expected_seq, prev, count, db_hashes = 1, GENESIS, 0, {}
            for seq, payload, prev_hash, digest, *cols in cur:
                count += 1
                db_hashes[seq] = digest
                if seq != expected_seq:
                    problems.append({"seq": seq, "problem": f"missing entries {expected_seq}..{seq - 1}"})
                if prev_hash != prev:
                    problems.append({"seq": seq, "problem": "prev_hash does not link to the previous entry"})
                if not hmac.compare_digest(_mac(prev_hash, payload), digest):
                    problems.append({"seq": seq, "problem": "content does not match its hash (entry altered)"})
                try:
                    body = json.loads(payload)
                    if body.get("seq") != seq:
                        problems.append({"seq": seq, "problem": "sequence number inside payload differs"})
                    for (name, getter), value in zip(_COLUMN_CHECKS, cols):
                        if getter(body) != value:
                            problems.append({"seq": seq, "problem": f"column '{name}' differs from signed payload"})
                except (ValueError, KeyError, TypeError):
                    problems.append({"seq": seq, "problem": "payload is not valid JSON"})
                expected_seq, prev = seq + 1, digest
            head_seq, head_hash = expected_seq - 1, prev
        conn.commit()
    finally:
        conn.close()

    file_index, bad_lines = _file_index()
    file_max = max(file_index) if file_index else 0
    if file_max > head_seq:
        problems.append({"seq": head_seq + 1,
                         "problem": f"the file copy has entries up to {file_max} but the table ends at {head_seq} "
                                    f"(entries were removed from the end of the table)"})
    mismatched = sorted(s for s, h in file_index.items() if s in db_hashes and h != db_hashes[s])
    for s in mismatched[:20]:
        problems.append({"seq": s, "problem": "file copy and table disagree for this entry"})
    missing_in_file = head_seq - len([s for s in file_index if s <= head_seq])
    return {"ok": not problems, "entries": count, "head_seq": head_seq, "head_hash": head_hash,
            "file_entries": len(file_index), "missing_in_file_copy": max(0, missing_in_file),
            "unreadable_file_lines": bad_lines, "problems": problems[:50],
            "checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}


# --------------------------------------------------------------------------- #
# Migration of the CHANGE-001 auth_events table into the chained log
# --------------------------------------------------------------------------- #
def migrate_auth_events():
    """One-off: copy legacy auth_events rows into audit_log (oldest first, original
    timestamps kept), then drop the old table so there is a single audit trail."""
    exists = db.query("SELECT to_regclass('public.auth_events') AS t", fetch="one")
    if not exists or not exists["t"]:
        return 0
    rows = db.query("""SELECT a.*, e.role FROM auth_events a LEFT JOIN employees e ON e.id = a.employee_id
                       ORDER BY a.created_at, a.id""")
    written = 0
    for r in rows:
        written += record("auth", r["event"], outcome=outcome_for_auth(r["event"]),
               actor={"id": r["employee_id"], "username": r["username"], "role": r["role"]},
               target_type="employee", target_id=r["employee_id"], target_label=r["username"],
               details={"detail": r["detail"], "migrated_from": "auth_events"}, ts=r["created_at"],
               ip=r["ip"], client="") is not None
    if written != len(rows):
        raise RuntimeError("audit migration incomplete — auth_events kept, nothing dropped")
    db.query("DROP TABLE auth_events", fetch=None)
    print(f"[audit] migrated {len(rows)} legacy auth_events row(s) into the audit chain", flush=True)
    return len(rows)


AUTH_FAILURES = {"login_failed", "login_blocked", "account_locked", "mfa_failed", "mfa_locked",
                 "password_change_failed"}


def outcome_for_auth(event):
    return "failure" if event in AUTH_FAILURES else "success"
