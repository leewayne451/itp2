"""Mandatory Job Rotation (MJR) tracker API  (CHANGE 006).

  GET  /api/rotations                    Director, HR: everyone on the rotation clock
  POST /api/rotations/<emp_id>/decision  Director: keep (reason) or rotate (department)
  GET  /api/rotations/planned            Requestor: rotations waiting for a shift request
  POST /api/rotations/check              HR: run the due check now (e-mails Directors)

The shift request itself is raised with the normal POST /api/requests (type 'shift')
plus "rotation_decision_id", so it follows the usual approval workflow.
"""
from flask import Blueprint, jsonify, request, g

from .. import rotation
from ..auth import require_role

bp = Blueprint("rotations_api", __name__)


@bp.get("/api/rotations")
@require_role("director", "hr")
def list_rotations():
    return jsonify(settings=rotation.settings(), officers=rotation.officers())


@bp.post("/api/rotations/<int:emp_id>/decision")
@require_role("director")
def decide(emp_id):
    body = request.get_json(silent=True) or {}
    try:
        o = rotation.decide(emp_id, body.get("decision"), body.get("reason"),
                            body.get("department_id"), g.user)
    except rotation.RotationError as e:
        return jsonify(error=e.message), e.status
    return jsonify(o)


@bp.get("/api/rotations/planned")
@require_role("requestor")
def planned():
    return jsonify(rotation.planned_rotations())


@bp.post("/api/rotations/check")
@require_role("hr")
def check_now():
    return jsonify(notified=rotation.check_due())
