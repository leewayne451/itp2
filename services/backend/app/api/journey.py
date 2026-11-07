"""Service journey analytics (CHANGE 007): read the latest results written by the
`analytics` container, and let staff ask for an immediate re-run.

The backend computes nothing here: it serves the journey_* tables (which it may only
read) and adds the names behind live requests, which the analytics role cannot see."""
from flask import Blueprint, request, jsonify

from .. import db
from ..auth import require_role
from ..core import STAFF_ROLES

bp = Blueprint("journey_api", __name__)
VIEWERS = STAFF_ROLES                     # hr, director, aa, it_admin, euss, admin
TYPES = ("all", "grant", "return", "shift", "onboard")


def _iso(v):
    return v.isoformat() if v else None


@bp.get("/api/journey")
@require_role(*VIEWERS)
def summary():
    rtype = request.args.get("type", "all")
    if rtype not in TYPES:
        return jsonify(error=f"type must be one of {', '.join(TYPES)}"), 400

    run = db.query("SELECT * FROM journey_runs WHERE status='ok' ORDER BY id DESC LIMIT 1", fetch="one")
    last_error = db.query("SELECT finished_at, error FROM journey_runs WHERE status='error' "
                          "AND id > COALESCE(%s, 0) ORDER BY id DESC LIMIT 1",
                          (run["id"] if run else None,), fetch="one")
    control = db.query("SELECT refresh_requested_at FROM journey_control WHERE id=1", fetch="one")
    if not run:
        return jsonify(run=None, stages=[], predictions=[], types=list(TYPES), type=rtype,
                       last_error=({"at": _iso(last_error["finished_at"]), "error": last_error["error"]}
                                   if last_error else None),
                       refresh_requested_at=_iso(control and control["refresh_requested_at"]))

    stages = db.query("SELECT * FROM journey_stage_stats WHERE run_id=%s AND request_type=%s",
                      (run["id"], rtype))
    # Live predictions get the request's people and current status; one whose request has
    # been closed since the run is dropped rather than shown as open.
    preds = db.query(
        """SELECT p.*, r.status AS current_status, r.on_behalf,
                  t.full_name AS target_name, rq.full_name AS requestor_name,
                  s.name AS service_name, nd.name AS new_department_name
           FROM journey_predictions p
           LEFT JOIN requests r     ON p.source = 'live' AND r.id = p.request_id
           LEFT JOIN employees t    ON t.id = r.target_employee_id
           LEFT JOIN employees rq   ON rq.id = r.requestor_id
           LEFT JOIN services s     ON s.id = r.service_id
           LEFT JOIN departments nd ON nd.id = r.target_department_id
           WHERE p.run_id = %s AND (%s = 'all' OR p.request_type = %s)
             AND (p.source = 'demo' OR r.status NOT IN ('completed', 'rejected'))
           ORDER BY CASE p.flag WHEN 'breached' THEN 0 WHEN 'at_risk' THEN 1 WHEN 'stuck' THEN 2 ELSE 3 END,
                    p.risk_miss DESC, p.elapsed_h DESC""",
        (run["id"], rtype, rtype))

    order = ["pending_hr", "pending_director", "pending_aa", "pending_it",
             "pending_appointment", "scheduled_wait", "pending_euss"]
    stages = sorted(stages, key=lambda s: order.index(s["stage"]) if s["stage"] in order else 99)
    return jsonify(
        type=rtype, types=list(TYPES),
        run={
            "id": run["id"], "finished_at": _iso(run["finished_at"]), "data_as_of": _iso(run["data_as_of"]),
            "live_requests": run["live_requests"], "demo_requests": run["demo_requests"],
            "events": run["events"], "open_requests": run["open_requests"],
            "bottleneck_stage": run["bottleneck_stage"], "backtest": run["backtest"] or {},
            "settings": run["settings"] or {},
        },
        last_error=({"at": _iso(last_error["finished_at"]), "error": last_error["error"]} if last_error else None),
        refresh_requested_at=_iso(control and control["refresh_requested_at"]),
        stages=[{k: s[k] for k in ("stage", "n", "median_h", "p75_h", "p90_h", "mean_h", "total_h",
                                   "share_pct", "wip", "oldest_open_h")} for s in stages],
        predictions=[{
            "source": p["source"], "request_id": p["request_id"], "request_type": p["request_type"],
            "stage": p["stage"], "stage_age_h": p["stage_age_h"], "elapsed_h": p["elapsed_h"],
            "target_h": p["target_h"], "remaining_p50_h": p["remaining_p50_h"],
            "remaining_p90_h": p["remaining_p90_h"], "eta_p50": _iso(p["eta_p50"]), "eta_p90": _iso(p["eta_p90"]),
            "risk_miss": p["risk_miss"], "flag": p["flag"], "stuck": p["stuck"], "history_n": p["history_n"],
            "target_name": p["target_name"], "requestor_name": p["requestor_name"],
            "service_name": p["service_name"], "new_department_name": p["new_department_name"],
        } for p in preds],
    )


@bp.post("/api/journey/refresh")
@require_role(*VIEWERS)
def refresh():
    """Ask the analytics container to run now (it checks every few seconds)."""
    db.query("UPDATE journey_control SET refresh_requested_at = now() WHERE id = 1", fetch=None)
    return jsonify(ok=True)
