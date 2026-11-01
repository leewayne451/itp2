"""Single sign-on with Keycloak: sign-in redirect, callback, sign-out (RP-initiated
and back-channel)."""
from flask import Blueprint, render_template, request, redirect, url_for, session, flash

from .. import token_store
from ..backend_client import call, AuthExpired
from ..oidc import OIDCError, ACCOUNT_URL
from ..oidc_client import client

bp = Blueprint("auth", __name__)


def _safe_next(target):
    """Only same-site relative paths (no open redirect)."""
    return target if target and target.startswith("/") and not target.startswith("//") else None


@bp.get("/login")
def login():
    """Signed in already -> dashboard. Otherwise go straight to Keycloak (if the user
    already has an SSO session there, they come back signed in without typing
    anything) — except right after signing out, where we show a landing page."""
    if session.get("user") and token_store.get(session.get("handle")):
        return redirect(url_for("dashboard.index"))
    if request.args.get("signed_out") or request.args.get("stay"):
        return render_template("login.html", signed_out=bool(request.args.get("signed_out")))
    session.pop("user", None)
    session.pop("handle", None)
    session["next"] = _safe_next(request.args.get("next"))
    try:
        return redirect(client().begin(session))
    except OIDCError as e:
        flash(str(e), "error")
        return render_template("login.html")


@bp.get("/callback")
def callback():
    try:
        tokens, id_claims = client().finish(session, request.args)
    except OIDCError as e:
        flash(str(e), "error")
        return redirect(url_for("auth.login", stay=1))
    access_exp = client().access_claims(tokens["access_token"]).get("exp", 0)
    # Ask the backend who this is: it validates the token, finds the linked employee,
    # checks the account is active and that staff signed in with TOTP.
    try:
        me = call("GET", "/api/me", token=tokens["access_token"])
    except AuthExpired as e:
        if e.reason == "Two-factor authentication required" and not session.get("stepped_up"):
            # Typically right after first-time TOTP set-up: Keycloak records the new
            # authenticator but not a code-verified sign-in. Ask for one fresh sign-in
            # (password + code) so the session is genuinely two-factor.
            nxt = session.get("next")
            session.clear()
            session["stepped_up"] = True
            session["next"] = nxt
            flash("Two-factor authentication is set up. Please sign in once more with a code from your app.", "ok")
            return redirect(client().begin(session, prompt="login"))
        flash(f"Signed in to Keycloak, but the console refused the session: {e.reason}", "error")
        return redirect(client().logout_url(tokens.get("id_token"), "/login?stay=1"))
    if not me.ok:
        flash("The console could not load your account.", "error")
        return redirect(url_for("auth.login", stay=1))
    nxt = _safe_next(session.pop("next", None))
    session.clear()
    session.permanent = True            # idle-timeout cookie lifetime
    session["handle"] = token_store.put(tokens, id_claims.get("sid"), access_exp)
    session["user"] = me.json()
    return redirect(nxt or url_for("dashboard.index"))


@bp.get("/logout")
def logout():
    """End the session here AND in Keycloak (which then signs the user out of every
    other app via back-channel logout)."""
    entry = token_store.drop(session.get("handle"))
    session.clear()
    return redirect(client().logout_url(entry and entry["id_token"], "/login?signed_out=1"))


@bp.post("/backchannel-logout")
def backchannel_logout():
    """Called server-to-server by Keycloak when the user's SSO session is ended (sign-out
    in any app, admin force sign-out). Keycloak does NOT call this when a session merely
    expires — the console then notices at its next token refresh."""
    try:
        claims = client().verify_logout_token(request.form.get("logout_token", ""))
    except OIDCError:
        return "", 400
    token_store.drop_kc_session(claims.get("sid"))
    return "", 200


@bp.app_context_processor
def _sso_links():
    return {"KEYCLOAK_ACCOUNT_URL": ACCOUNT_URL}
