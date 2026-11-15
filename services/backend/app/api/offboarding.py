"""HR/IT endpoints for scheduled and emergency leaver cut-off."""
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from flask import Blueprint, g, jsonify, request

from .. import offboarding
from ..auth import require_role

bp = Blueprint("offboarding_api", __name__)
SGT = ZoneInfo("Asia/Singapore")


def parse_effective_at(value):
    if not isinstance(value, str) or not value.strip():
        raise ValueError("Effective date and time is required")
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("Effective date and time is invalid") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=SGT)
    parsed = parsed.astimezone(timezone.utc)
    if parsed <= datetime.now(timezone.utc):
        raise ValueError("Scheduled offboarding must be in the future")
    return parsed


def _reason(body):
    reason = (body.get("reason") or "").strip()
    if len(reason) < 3:
        raise ValueError("A reason of at least 3 characters is required")
    if len(reason) > 500:
        raise ValueError("Reason must not exceed 500 characters")
    return reason


@bp.post("/api/employees/<int:emp_id>/offboarding/schedule")
@require_role("hr", "it_admin")
def schedule_offboarding(emp_id):
    body = request.get_json(silent=True) or {}
    try:
        effective_at = parse_effective_at(body.get("effective_at"))
        row = offboarding.schedule(emp_id, effective_at, _reason(body), g.user)
    except (ValueError, offboarding.OffboardingError) as exc:
        return jsonify(error=str(exc)), 409
    return jsonify(ok=True, employee_id=emp_id,
                   scheduled_offboard_at=row["scheduled_offboard_at"].isoformat())


@bp.delete("/api/employees/<int:emp_id>/offboarding/schedule")
@require_role("hr", "it_admin")
def cancel_scheduled_offboarding(emp_id):
    body = request.get_json(silent=True) or {}
    try:
        offboarding.cancel_schedule(emp_id, _reason(body), g.user)
    except (ValueError, offboarding.OffboardingError) as exc:
        return jsonify(error=str(exc)), 409
    return jsonify(ok=True, employee_id=emp_id)


@bp.post("/api/employees/<int:emp_id>/offboard")
@require_role("it_admin")
def emergency_offboard(emp_id):
    body = request.get_json(silent=True) or {}
    try:
        result = offboarding.execute(emp_id, _reason(body), g.user, trigger="emergency")
    except (ValueError, offboarding.OffboardingError) as exc:
        return jsonify(error=str(exc)), 409
    return jsonify(ok=True, **result)
