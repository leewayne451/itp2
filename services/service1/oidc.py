"""
OpenID Connect client for Keycloak single sign-on (shared by the console and the
two protected services — kept as one self-contained file in each app).

Flow: authorization code + PKCE (S256) + state + nonce, confidential client.
  * the browser is sent to Keycloak's PUBLIC URL (that is also the token issuer);
  * the code is exchanged and keys are fetched over the INTERNAL docker URL,
    with TLS verified against our CA;
  * ID tokens and back-channel logout tokens are signature-checked (RS256, JWKS),
    plus issuer, audience, expiry, nonce / logout-event claims.
"""
import base64
import hashlib
import os
import secrets
import ssl
import time
from urllib.parse import urlencode

import jwt
import requests

KEYCLOAK_URL = os.environ.get("KEYCLOAK_URL", "https://keycloak:8443").rstrip("/")
KEYCLOAK_PUBLIC_URL = os.environ.get("KEYCLOAK_PUBLIC_URL", "https://localhost:8443").rstrip("/")
KEYCLOAK_REALM = os.environ.get("KEYCLOAK_REALM", "servicejourney")
CA_BUNDLE = os.environ.get("CA_BUNDLE", "/certs/ca.crt")

ISSUER = f"{KEYCLOAK_PUBLIC_URL}/realms/{KEYCLOAK_REALM}"
_PUBLIC = f"{ISSUER}/protocol/openid-connect"
_INTERNAL = f"{KEYCLOAK_URL}/realms/{KEYCLOAK_REALM}/protocol/openid-connect"
AUTHORIZE_URL = f"{_PUBLIC}/auth"
LOGOUT_URL = f"{_PUBLIC}/logout"
ACCOUNT_URL = f"{ISSUER}/account"
TOKEN_URL = f"{_INTERNAL}/token"
JWKS_URL = f"{_INTERNAL}/certs"
BACKCHANNEL_LOGOUT_EVENT = "http://schemas.openid.net/event/backchannel-logout"


def _verify():
    return CA_BUNDLE if os.path.exists(CA_BUNDLE) else True


class OIDCError(Exception):
    """`unreachable` is True when Keycloak could not be reached (network error or a
    5xx) — callers may then fail open — and False when Keycloak answered "no"
    (e.g. the refresh token is no longer valid because the user was disabled or
    the SSO session ended), which must always end the session."""

    def __init__(self, message, unreachable=False):
        super().__init__(message)
        self.unreachable = unreachable


class OIDCClient:
    def __init__(self, client_id, client_secret, app_url):
        if len(client_secret or "") < 32:
            raise RuntimeError("OIDC_CLIENT_SECRET is missing or too weak. Run `python scripts/generate_secrets.py`.")
        self.client_id = client_id
        self.client_secret = client_secret
        self.app_url = app_url.rstrip("/")
        self.redirect_uri = f"{self.app_url}/callback"
        ctx = ssl.create_default_context(cafile=CA_BUNDLE if os.path.exists(CA_BUNDLE) else None)
        self._jwks = jwt.PyJWKClient(JWKS_URL, cache_keys=True, lifespan=300, timeout=10, ssl_context=ctx)
        self._http = requests.Session()

    # ------------------------------------------------------------- sign-in
    def begin(self, store, prompt=None):
        """Create state/nonce/PKCE, remember them in `store` (the Flask session) and
        return the Keycloak URL to redirect the browser to. prompt="login" forces a
        fresh sign-in (password + second factor) even inside an SSO session."""
        verifier = secrets.token_urlsafe(64)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        state, nonce = secrets.token_urlsafe(24), secrets.token_urlsafe(24)
        store["oidc"] = {"state": state, "nonce": nonce, "verifier": verifier, "t": time.time()}
        params = {"client_id": self.client_id, "response_type": "code", "scope": "openid",
                  "redirect_uri": self.redirect_uri, "state": state, "nonce": nonce,
                  "code_challenge": challenge, "code_challenge_method": "S256"}
        if prompt:
            params["prompt"] = prompt
        return AUTHORIZE_URL + "?" + urlencode(params)

    def finish(self, store, args):
        """Handle the redirect back from Keycloak. Returns (tokens, id_claims)."""
        pending = store.pop("oidc", None)
        if args.get("error"):
            raise OIDCError(args.get("error_description") or args["error"])
        if not pending or time.time() - pending["t"] > 600:
            raise OIDCError("Sign-in expired or was started in another tab. Please try again.")
        if not secrets.compare_digest(args.get("state", ""), pending["state"]):
            raise OIDCError("Invalid sign-in state.")
        tokens = self._token_request({"grant_type": "authorization_code", "code": args.get("code", ""),
                                      "redirect_uri": self.redirect_uri, "code_verifier": pending["verifier"]})
        claims = self.verify_id_token(tokens.get("id_token", ""))
        if not secrets.compare_digest(claims.get("nonce", ""), pending["nonce"]):
            raise OIDCError("Invalid sign-in nonce.")
        return tokens, claims

    def refresh(self, refresh_token):
        return self._token_request({"grant_type": "refresh_token", "refresh_token": refresh_token})

    def _token_request(self, data):
        data = dict(data, client_id=self.client_id, client_secret=self.client_secret)
        try:
            r = self._http.post(TOKEN_URL, data=data, timeout=10, verify=_verify())
        except requests.RequestException as e:
            raise OIDCError("The sign-in service (Keycloak) is not reachable.", unreachable=True) from e
        if r.status_code >= 500:
            raise OIDCError(f"The sign-in service (Keycloak) is unavailable ({r.status_code}).", unreachable=True)
        if r.status_code != 200:
            try:
                msg = r.json().get("error_description") or r.json().get("error")
            except ValueError:
                msg = r.text[:200]
            raise OIDCError(f"Sign-in failed: {msg}")
        return r.json()

    # ------------------------------------------------------------- tokens
    def _decode(self, token, **kw):
        try:
            key = self._jwks.get_signing_key_from_jwt(token).key
            return jwt.decode(token, key, algorithms=["RS256"], audience=self.client_id, issuer=ISSUER,
                              leeway=30, **kw)
        except (jwt.InvalidTokenError, jwt.PyJWKClientError) as e:
            raise OIDCError(f"Invalid token from the sign-in service ({type(e).__name__})") from e

    def verify_id_token(self, token):
        claims = self._decode(token, options={"require": ["exp", "iat", "sub", "aud", "iss"]})
        if claims.get("typ") not in (None, "ID"):
            raise OIDCError("Not an ID token")
        return claims

    def verify_logout_token(self, token):
        """OIDC Back-Channel Logout 1.0 §2.6 checks."""
        claims = self._decode(token, options={"require": ["iat", "aud", "iss"], "verify_exp": False})
        if BACKCHANNEL_LOGOUT_EVENT not in (claims.get("events") or {}):
            raise OIDCError("Not a logout token")
        if "nonce" in claims or not (claims.get("sid") or claims.get("sub")):
            raise OIDCError("Malformed logout token")
        return claims

    @staticmethod
    def access_claims(access_token):
        """Read (not verify) our own access token's claims — e.g. its expiry. The
        backend API is what verifies access tokens."""
        return jwt.decode(access_token, options={"verify_signature": False})

    # ------------------------------------------------------------- sign-out
    def logout_url(self, id_token, post_logout_path="/"):
        params = {"client_id": self.client_id, "post_logout_redirect_uri": self.app_url + post_logout_path}
        if id_token:
            params["id_token_hint"] = id_token
        return LOGOUT_URL + "?" + urlencode(params)
