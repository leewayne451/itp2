"""The console's single OIDC client instance (created lazily from config)."""
from . import config
from .oidc import OIDCClient

_client = {}


def client():
    if "c" not in _client:
        _client["c"] = OIDCClient(config.OIDC_CLIENT_ID, config.OIDC_CLIENT_SECRET, config.APP_URL)
    return _client["c"]
