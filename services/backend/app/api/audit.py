"""Read-only access to the audit trail for IT administrators: search, integrity
check and download. There is deliberately NO endpoint that edits or deletes."""
import csv
import hashlib
import io
import json
from datetime import datetime, timezone

from flask import Blueprint, request, jsonify, Response

from .. import audit, db, kc_events
from ..auth import require_role

bp = Blueprint("audit_api", __name__)
AUDITORS = ("it_admin",)
MAX_EXPORT = 100_000
_CSV_COLUMNS = ["seq", "ts", "category", "action", "outcome", "actor_username", "actor_role", "actor_id",
                "target_type", "target_id", "target_label", "ip", "client", "details", "prev_hash", "hash"]


def _filters():
    """WHERE clause + params from the query string (all optional)."""
    a = request.args
    where, params = [], []
    if a.get("category") in audit.CATEGORIES:
        where.append("category = %s"); params.append(a["category"])
    if a.get("outcome") in ("success", "failure", "denied"):
        where.append("outcome = %s"); params.append(a["outcome"])
    for field, column in (("action", "action"), ("actor", "actor_username"), ("target", "target_label")):
        if a.get(field):
            where.append(f"{column} ILIKE %s"); params.append(f"%{a[field].strip()}%")
    if a.get("q"):
        where.append("payload ILIKE %s"); params.append(f"%{a['q'].strip()}%")
    for field, op in (("since", ">="), ("until", "<")):
        if a.get(field):
            try:
                day = datetime.strptime(a[field], "%Y-%m-%d")
            except ValueError:
                continue
            where.append(f"ts {op} %s::date" + (" + 1" if field == "until" else ""))
            params.append(day.date().isoformat())
    return (" WHERE " + " AND ".join(where)) if where else "", params


def _entry(row):
    payload = json.loads(row["payload"])
    return {"seq": row["seq"], "ts": payload["ts"], "category": row["category"], "action": row["action"],
            "outcome": row["outcome"], "actor": payload["actor"], "target": payload["target"],
            "ip": payload.get("ip"), "client": payload.get("client"), "details": payload.get("details") or {},
            "hash": row["hash"], "prev_hash": row["prev_hash"]}


@bp.get("/api/audit")
@require_role(*AUDITORS)
def list_entries():
    kc_events.ingest()          # include Keycloak sign-in events up to this moment
    where, params = _filters()
    try:
        page = max(1, int(request.args.get("page", 1)))
        size = max(1, min(int(request.args.get("page_size", 50)), 200))
    except ValueError:
        page, size = 1, 50
    total = db.query("SELECT COUNT(*) AS c FROM audit_log" + where, tuple(params), fetch="one")["c"]
    rows = db.query("SELECT * FROM audit_log" + where + " ORDER BY seq DESC LIMIT %s OFFSET %s",
                    tuple(params) + (size, (page - 1) * size))
    return jsonify(total=total, page=page, page_size=size, entries=[_entry(r) for r in rows],
                   categories=list(audit.CATEGORIES))


@bp.get("/api/audit/verify")
@require_role(*AUDITORS)
def verify():
    kc_events.ingest()
    report = audit.verify()
    audit.record("audit", "integrity_verified", outcome="success" if report["ok"] else "failure",
                 details={"entries": report["entries"], "head_seq": report["head_seq"],
                          "problems": len(report["problems"])})
    return jsonify(report)


def _csv_safe(value):
    """Neutralise spreadsheet formula injection (OWASP CSV Injection)."""
    text = "" if value is None else str(value)
    return "'" + text if text[:1] in ("=", "+", "-", "@", "\t", "\r") else text


@bp.get("/api/audit/export")
@require_role(*AUDITORS)
def export():
    """Download the (optionally filtered) trail as CSV or JSON Lines. The JSONL
    form carries each entry's signed payload + hashes, so a copy can be
    re-verified independently later. The export itself is audited."""
    fmt = request.args.get("format", "csv")
    if fmt not in ("csv", "jsonl"):
        return jsonify(error="format must be csv or jsonl"), 400
    kc_events.ingest()
    where, params = _filters()
    rows = db.query("SELECT * FROM audit_log" + where + " ORDER BY seq LIMIT %s", tuple(params) + (MAX_EXPORT,))
    if fmt == "jsonl":
        body = "".join(json.dumps({"seq": r["seq"], "prev_hash": r["prev_hash"], "hash": r["hash"],
                                   "payload": json.loads(r["payload"])}, sort_keys=True, ensure_ascii=False) + "\n"
                       for r in rows)
        mimetype = "application/x-ndjson"
    else:
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(_CSV_COLUMNS)
        for r in rows:
            e = _entry(r)
            w.writerow([_csv_safe(v) for v in (
                e["seq"], e["ts"], e["category"], e["action"], e["outcome"], e["actor"]["username"],
                e["actor"]["role"], e["actor"]["id"], e["target"]["type"], e["target"]["id"], e["target"]["label"],
                e["ip"], e["client"], json.dumps(e["details"], sort_keys=True, ensure_ascii=False),
                e["prev_hash"], e["hash"])])
        body = buf.getvalue()
        mimetype = "text/csv"
    data = body.encode("utf-8")
    digest = hashlib.sha256(data).hexdigest()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    filename = f"servicejourney-audit-{stamp}.{fmt}"
    audit.record("audit", "exported", details={"format": fmt, "records": len(rows), "sha256": digest,
                                               "filters": {k: v for k, v in request.args.items() if k != "format"}})
    resp = Response(data, mimetype=mimetype)
    resp.headers["Content-Disposition"] = f'attachment; filename="{filename}"'
    resp.headers["X-Audit-Records"] = str(len(rows))
    resp.headers["X-Audit-SHA256"] = digest
    return resp
