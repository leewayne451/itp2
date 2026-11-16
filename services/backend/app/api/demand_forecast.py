"""Read-only demand-forecast endpoint for capacity planning."""
from flask import Blueprint, jsonify

from ..auth import require_role
from ..demand_forecast import build_forecast

bp = Blueprint("demand_forecast_api", __name__)


@bp.get("/api/demand-forecast")
@require_role("it_admin")
def forecast():
    return jsonify(build_forecast())
