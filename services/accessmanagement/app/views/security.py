"""Security page: what protects this sign-in, and links to the Keycloak account
console where users change their password, set up / remove their authenticator
app and see or end their sessions (single sign-on: credentials live in Keycloak)."""
from datetime import datetime

from flask import Blueprint, render_template

from . import login_required
from ..backend_client import get_json
from ..labels import SGT
from ..oidc import ACCOUNT_URL

bp = Blueprint("security", __name__)


@bp.get("/security")
@login_required
def index():
    me = get_json("/api/me", default={}) or {}
    if isinstance(me.get("auth_time"), (int, float)):
        me["auth_time"] = datetime.fromtimestamp(me["auth_time"], tz=SGT).strftime("%Y-%m-%d %H:%M SGT")
    links = {
        "signing_in": f"{ACCOUNT_URL}/account-security/signing-in",
        "devices": f"{ACCOUNT_URL}/account-security/device-activity",
        "personal": f"{ACCOUNT_URL}/",
    }
    return render_template("security.html", me=me, links=links)
