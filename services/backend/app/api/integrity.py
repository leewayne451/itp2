"""Mandatory Block Leave (leave clearance) + casino visit declarations API  (CHANGE 012).

  GET  /api/integrity                          Director, HR: settings + MBL list + casino list + declarations
  GET  /api/integrity/officers                 IT Admin, HR, Director: officers + sensitive-position flag
  POST /api/integrity/officers/<id>/sensitive  IT Admin: {"sensitive": true|false, "reason": ...}
  POST /api/mbl/entitlements                   HR: {employee_id, year, entitled_days, carried_forward_days}
  POST /api/mbl/leaves                         HR: record leave {employee_id, start_date, end_date, note}
  POST /api/mbl/leaves/<id>/cancel             HR: cancel a leave record
  POST /api/mbl/remind                         HR: e-mail officers who still have leave to clear
  GET  /api/casino/me                          any officer: own rule + declarations (+ own leave to clear)
  POST /api/casino/declarations                employee (self) or HR (employee_id): declare a visit / annual levy
"""
from flask import Blueprint, jsonify, request, g

from .. import integrity
from ..auth import require_auth, require_role

bp = Blueprint("integrity_api", __name__)


def _err(e):
    return jsonify(error=e.message), e.status


def _emp_id(body):
    try:
        return int(body.get("employee_id") or 0)
    except (TypeError, ValueError):
        return 0


@bp.get("/api/integrity")
@require_role("director", "hr")
def overview():
    year = request.args.get("year", type=int)
    return jsonify(settings=integrity.settings(), mbl=integrity.mbl_officers(year),
                   casino=integrity.casino_officers(), declarations=integrity.recent_declarations())


@bp.get("/api/integrity/officers")
@require_role("it_admin", "hr", "director")
def list_officers():
    return jsonify(integrity.officers())


@bp.post("/api/integrity/officers/<int:emp_id>/sensitive")
@require_role("it_admin")
def set_sensitive(emp_id):
    body = request.get_json(silent=True) or {}
    try:
        return jsonify(integrity.set_sensitive(emp_id, body.get("sensitive"), body.get("reason"), g.user))
    except integrity.IntegrityError as e:
        return _err(e)


@bp.post("/api/mbl/entitlements")
@require_role("hr")
def set_entitlement():
    body = request.get_json(silent=True) or {}
    try:
        return jsonify(integrity.set_entitlement(_emp_id(body), body.get("year") or integrity._today().year,
                                                 body.get("entitled_days"), body.get("carried_forward_days"), g.user))
    except integrity.IntegrityError as e:
        return _err(e)


@bp.post("/api/mbl/leaves")
@require_role("hr")
def record_leave():
    body = request.get_json(silent=True) or {}
    try:
        return jsonify(integrity.record_leave(_emp_id(body), body.get("start_date"), body.get("end_date"),
                                              body.get("note"), g.user)), 201
    except integrity.IntegrityError as e:
        return _err(e)


@bp.post("/api/mbl/leaves/<int:record_id>/cancel")
@require_role("hr")
def cancel_leave(record_id):
    try:
        return jsonify(integrity.cancel_leave(record_id, g.user))
    except integrity.IntegrityError as e:
        return _err(e)


@bp.post("/api/mbl/remind")
@require_role("hr")
def remind():
    return jsonify(reminded=integrity.remind_mbl(g.user))


@bp.get("/api/casino/me")
@require_auth
def my_casino():
    s = integrity.officer_summary(g.user["id"])
    if s is None:
        return jsonify(in_scope=False, settings=integrity.settings())
    s.update(in_scope=True, settings=integrity.settings())
    return jsonify(s)


@bp.post("/api/casino/declarations")
@require_auth
def declare():
    body = request.get_json(silent=True) or {}
    role = g.user.get("role")
    if role == "hr" and body.get("employee_id"):
        emp_id = _emp_id(body)
    elif role == "employee":
        if body.get("employee_id") and str(body["employee_id"]) != str(g.user["id"]):
            return jsonify(error="You can only declare your own visits"), 403
        emp_id = g.user["id"]
    else:
        return jsonify(error="Only the officer, or HR on the officer's behalf, can declare"), 403
    try:
        return jsonify(integrity.declare(emp_id, body.get("kind") or "visit", body.get("visit_date"),
                                         body.get("casino"), body.get("official_duty"), body.get("purpose"),
                                         g.user)), 201
    except integrity.IntegrityError as e:
        return _err(e)
