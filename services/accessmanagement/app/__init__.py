"""AccessManagement console — server-rendered application factory."""
from datetime import timedelta
from urllib.parse import urlsplit

from flask import Flask, session, redirect, url_for, request, abort, flash

from . import config, labels
from .backend_client import AuthExpired
from .views import (auth, dashboard, requests as req_views, admin, quotas, assets,
                    security, audit, rotations, journey, access_reviews, tracker)


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
    # Idle timeout: the cookie lifetime is renewed on every request, so it lapses
    # only after IDLE_TIMEOUT_MINUTES without activity.
    app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(minutes=config.IDLE_TIMEOUT_MINUTES)
    app.config["SESSION_REFRESH_EACH_REQUEST"] = True

    @app.before_request
    def _csrf_origin_check():
        """CSRF defence (OWASP: verify origin with standard headers) on top of
        SameSite=Lax: a state-changing request must come from this console's own
        pages. Browsers always send Origin on POST; Referer is the fallback."""
        if request.method in ("GET", "HEAD", "OPTIONS"):
            return None
        if request.path == "/backchannel-logout":
            return None   # server-to-server from Keycloak; authenticated by its signed logout token
        source = request.headers.get("Origin") or request.headers.get("Referer")
        if not source or source == "null":
            abort(403)
        src = urlsplit(source)
        here = urlsplit(request.host_url)
        if (src.scheme, src.netloc) != (here.scheme, here.netloc):
            abort(403)
        return None

    @app.after_request
    def _security_headers(resp):
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("X-Frame-Options", "DENY")              # no clickjacking of the console
        # Never send our URLs (e.g. a reset-link token) to other sites. ("no-referrer"
        # would also make browsers send `Origin: null`, defeating the check above.)
        resp.headers.setdefault("Referrer-Policy", "same-origin")
        resp.headers.setdefault("Cache-Control", "no-store")            # pages show codes / temp passwords
        return resp

    for module in (auth, dashboard, req_views, admin, quotas, assets, security,
                   audit, rotations, journey, access_reviews, tracker):
        app.register_blueprint(module.bp)

    labels.register(app)

    @app.errorhandler(AuthExpired)
    def _session_expired(e):
        from . import token_store
        token_store.drop(session.get("handle"))
        session.clear()
        flash(f"{e.reason or 'Your session has ended'}. Please sign in again.", "error")
        return redirect(url_for("auth.login"))

    @app.context_processor
    def _inject_user():
        return {"current_user": session.get("user")}

    return app
