"""Backend application factory."""
from flask import Flask

from . import db
from .api import auth, reference, admin, quotas, requests as requests_api


def create_app():
    app = Flask(__name__)
    for module in (auth, reference, admin, quotas, requests_api):
        app.register_blueprint(module.bp)
    return app


def bootstrap():
    """Wait for Postgres, then apply schema + seed. Call before serving."""
    db.wait_for_db()
    db.init_db()
