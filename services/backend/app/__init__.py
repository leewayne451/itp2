"""Backend application factory."""
from flask import Flask

from . import audit, db, passwords, sync
from .clients import authenticate_client
from .api import auth, reference, admin, quotas, requests as requests_api, audit as audit_api, rotations, journey, hr


def create_app():
    app = Flask(__name__)
    app.config["MAX_CONTENT_LENGTH"] = 64 * 1024 * 1024   # bound request bodies (documents are base64)
    app.before_request(authenticate_client)
    for module in (auth, reference, admin, quotas, requests_api, audit_api, rotations, journey, hr):
        app.register_blueprint(module.bp)

    @app.after_request
    def _no_store(resp):
        # Responses carry temporary passwords and personal data: never cache.
        resp.headers["Cache-Control"] = "no-store"
        return resp

    return app


def bootstrap():
    """Wait for Postgres, then apply schema, migrate legacy plaintext passwords,
    and seed. Call before serving."""
    db.wait_for_db()
    db.init_db(migrations=(passwords.migrate_plaintext_passwords, audit.migrate_auth_events))


def start_background():
    """Keycloak user sync (with credential migration) + Keycloak event ingestion
    + the Mandatory Job Rotation due check (CHANGE 006)."""
    sync.start_background()
