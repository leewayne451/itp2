"""AccessManagement console — server-rendered application factory."""
from flask import Flask, session, redirect, url_for

from . import config, labels
from .backend_client import AuthExpired
from .views import auth, dashboard, requests as req_views, admin, quotas, assets


def create_app():
    app = Flask(__name__)
    app.secret_key = config.SECRET_KEY
    app.config["BACKEND_URL"] = config.BACKEND_URL
    app.config["SESSION_COOKIE_NAME"] = config.SESSION_COOKIE_NAME
    app.config["MAX_CONTENT_LENGTH"] = 8 * 1024 * 1024  # cap document uploads at 8 MB
    # Now that everything is served over TLS, keep the session cookie off any
    # plaintext connection and away from JavaScript.
    app.config["SESSION_COOKIE_SECURE"] = config.tls_enabled()
    app.config["SESSION_COOKIE_HTTPONLY"] = True
    app.config["SESSION_COOKIE_SAMESITE"] = "Lax"

    for module in (auth, dashboard, req_views, admin, quotas, assets):
        app.register_blueprint(module.bp)

    labels.register(app)

    @app.errorhandler(AuthExpired)
    def _session_expired(_e):
        session.clear()
        return redirect(url_for("auth.login"))

    @app.context_processor
    def _inject_user():
        return {"current_user": session.get("user")}

    return app
