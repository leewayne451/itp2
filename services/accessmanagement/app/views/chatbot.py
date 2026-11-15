"""Authenticated chat assistant endpoints."""
import requests
from flask import Blueprint, abort, current_app, g, jsonify, render_template, request, url_for

from . import login_required
from ..backend_client import call, error_of
from ..chatbot import action_for, guidance_for, intent_for, request_id_for, status_reply
from ..demo_policy import mock_policy_answer

bp = Blueprint("chatbot", __name__)
MAX_MESSAGE_LENGTH = 500
QUEUE_ROLES = {"requestor", "hr", "director", "aa", "it_admin", "euss"}
DETAIL_ROLES = {"hr", "director", "aa", "it_admin", "euss", "admin"}


def _status_sources(role):
    if role == "admin":
        return [("/api/requests/my-status", None), ("/api/requests", None)]
    sources = [("/api/requests/my-status", None)]
    if role in QUEUE_ROLES:
        sources.extend((("/api/requests/queue", None), ("/api/requests/history", None)))
    return sources


def _get_json(path, params=None):
    try:
        response = call("GET", path, params=params)
    except requests.RequestException:
        current_app.logger.exception("Chat assistant could not retrieve request status")
        return None, ("I couldn't reach the request service. Please try again shortly.", 503)
    if not response.ok:
        status = response.status_code if response.status_code < 500 else 502
        return None, (error_of(response), status)
    try:
        data = response.json()
    except ValueError:
        return None, ("The request service returned an invalid response.", 502)
    return data, None


def _status_rows(role, request_id=None):
    if request_id is not None:
        rows, error = _get_json("/api/requests/my-status", {"request_id": request_id})
        if error:
            return None, error
        if not isinstance(rows, list):
            return None, ("The request service returned an invalid response.", 502)
        if rows:
            return rows, error
        if role in DETAIL_ROLES:
            detail, error = _get_json(f"/api/requests/{request_id}")
            if error and error[1] in (403, 404):
                return [], None
            if error is None and not isinstance(detail, dict):
                return None, ("The request service returned an invalid response.", 502)
            return ([detail] if error is None else None), error
        return [], None

    combined = {}
    for path, params in _status_sources(role):
        rows, error = _get_json(path, params)
        if error:
            return None, error
        if not isinstance(rows, list):
            return None, ("The request service returned an invalid response.", 502)
        for row in rows:
            combined[row["id"]] = row
    return sorted(combined.values(), key=lambda row: row.get("updated_at", ""), reverse=True), None


@bp.post("/chat")
@login_required
def chat():
    body = request.get_json(silent=True)
    if not isinstance(body, dict) or not isinstance(body.get("message"), str):
        return jsonify(error="Send a message as JSON in the 'message' field."), 400
    message = body["message"].strip()
    if not message:
        return jsonify(error="Enter a message first."), 400
    if len(message) > MAX_MESSAGE_LENGTH:
        return jsonify(error=f"Messages must be {MAX_MESSAGE_LENGTH} characters or fewer."), 413

    intent = intent_for(message)
    if intent == "status":
        request_id = request_id_for(message)
        requests_data, error = _status_rows(g.user["role"], request_id)
        if error:
            return jsonify(error=error[0]), error[1]
        return jsonify(reply=status_reply(requests_data, request_id), actions=[])
    if intent == "policy":
        return jsonify(reply=mock_policy_answer(message), actions=[])

    dashboard_url = url_for("dashboard.index")
    return jsonify(
        reply=guidance_for(intent, g.user["role"]),
        actions=action_for(intent, g.user["role"], dashboard_url),
    )


@bp.get("/preview")
def preview():
    if not current_app.config.get("CHATBOT_PREVIEW_ENABLED", False):
        abort(404)
    return render_template("chatbot_preview.html", chatbot_preview=True)


@bp.post("/chat/preview")
def preview_chat():
    if not current_app.config.get("CHATBOT_PREVIEW_ENABLED", False):
        abort(404)
    body = request.get_json(silent=True)
    if not isinstance(body, dict) or not isinstance(body.get("message"), str):
        return jsonify(error="Send a message as JSON in the 'message' field."), 400
    message = body["message"].strip()
    if not message:
        return jsonify(error="Enter a message first."), 400
    if len(message) > MAX_MESSAGE_LENGTH:
        return jsonify(error=f"Messages must be {MAX_MESSAGE_LENGTH} characters or fewer."), 413

    intent = intent_for(message)
    if intent == "status":
        reply = ("Demo only: this preview is not connected to a signed-in account or live request "
                 "data. Sign in to the console to check your request status.")
    else:
        reply = mock_policy_answer(message) or f"Demo only: {guidance_for(intent, 'employee')}"
    return jsonify(reply=reply, actions=[])
