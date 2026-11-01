"""
Validation of Keycloak-issued access tokens (OIDC / OAuth 2.0 bearer tokens).

A token is accepted only if ALL of these hold:
  * RS256 signature by one of the realm's current keys (fetched from the JWKS
    endpoint over verified TLS, cached and refreshed on key rotation);
  * issuer is our realm, audience includes this API, it is an access token
    (typ=Bearer) and the authorised party (azp) is an app allowed to call us;
  * not expired / not used before its issue time (30 s clock-skew allowance).
"""
import os
import ssl

import jwt

from .config import KEYCLOAK_ISSUER, KEYCLOAK_JWKS_URL, API_AUDIENCE, API_AUTHORIZED_PARTIES, CA_BUNDLE

_ctx = ssl.create_default_context(cafile=CA_BUNDLE if os.path.exists(CA_BUNDLE) else None)
_jwks = jwt.PyJWKClient(KEYCLOAK_JWKS_URL, cache_keys=True, lifespan=300, timeout=10, ssl_context=_ctx)


class InvalidToken(Exception):
    pass


def validate(token):
    """Claims of a valid access token, else raise InvalidToken(reason)."""
    try:
        key = _jwks.get_signing_key_from_jwt(token).key
        claims = jwt.decode(token, key, algorithms=["RS256"], audience=API_AUDIENCE, issuer=KEYCLOAK_ISSUER,
                            leeway=30, options={"require": ["exp", "iat", "iss", "sub", "aud"]})
    except jwt.ExpiredSignatureError as e:
        raise InvalidToken("Token expired") from e
    except jwt.PyJWKClientError as e:
        raise InvalidToken("Signing key unavailable (is Keycloak running?)") from e
    except jwt.InvalidTokenError as e:
        raise InvalidToken(f"Invalid token ({type(e).__name__})") from e
    if claims.get("typ") != "Bearer":
        raise InvalidToken("Not an access token")
    if claims.get("azp") not in API_AUTHORIZED_PARTIES:
        raise InvalidToken("This application may not call the API")
    return claims
