"""IT Admin endpoint for emergency leaver cut-off."""

from flask import Blueprint, g, jsonify, request

from .. import offboarding
from ..auth import require_role

bp = Blueprint("offboarding_api", __name__)


def _reason(body):
    reason = (body.get("reason") or "").strip()
    if len(reason) < 3:
        raise ValueError("A reason of at least 3 characters is required")
    if len(reason) > 500:
        raise ValueError("Reason must not exceed 500 characters")
    return reason


@bp.post("/api/employees/<int:emp_id>/offboard")
@require_role("it_admin")
def emergency_offboard(emp_id):
    body = request.get_json(silent=True) or {}
    try:
        result = offboarding.execute(emp_id, _reason(body), g.user, trigger="emergency")
    except (ValueError, offboarding.OffboardingError) as exc:
        return jsonify(error=str(exc)), 409
    return jsonify(ok=True, **result)
