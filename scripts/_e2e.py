"""Shared helpers for the end-to-end test scripts (test_sso.py, test_audit.py).

They run from the host against the RUNNING docker stack:
  * the backend API      https://localhost:5000  (with the console's client credential)
  * Keycloak             https://localhost:8443  (sign-in forms, token endpoint, admin API)
  * the three apps       https://localhost:8080 / 8081 / 8082

Signing in is done the way a browser does it: the scripts fetch Keycloak's login
page and submit its forms (password, TOTP set-up, one-time code, new password),
then exchange the authorization code (+PKCE) for tokens — no password grant.

To make the scripts re-runnable, `prepare()` uses the Keycloak bootstrap admin
(kcadmin, password from .env) to put a demo account into a known state first:
authenticator removed, lock-out cleared, a random temporary password set. So the
scripts REPLACE the passwords of the demo accounts they use — run them on a demo
stack only. Secrets are read from .env and never printed.
"""
import base64
import hashlib
import hmac
import html
import os
import re
import secrets
import string
import struct
import time
import urllib.parse

import requests

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
BASE = os.environ.get("BACKEND", "https://localhost:5000")
KEYCLOAK = os.environ.get("KEYCLOAK", "https://localhost:8443").rstrip("/")
REALM = "servicejourney"
CA = os.environ.get("CA_BUNDLE", os.path.join(ROOT, "certs", "ca.crt"))
APPS = {"console": "https://localhost:8080", "service1": "https://localhost:8081", "service2": "https://localhost:8082"}
OIDC = f"{KEYCLOAK}/realms/{REALM}/protocol/openid-connect"
ADMIN = f"{KEYCLOAK}/admin/realms/{REALM}"


def _load_env():
    env = {}
    path = os.path.join(ROOT, ".env")
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    env[k.strip()] = v.strip()
    env.update({k: v for k, v in os.environ.items() if k.endswith(("_SECRET", "_PASSWORD", "_KEY"))})
    return env


ENV = _load_env()
SECRETS = {"console": ENV.get("CONSOLE_CLIENT_SECRET"), "service1": ENV.get("SERVICE1_CLIENT_SECRET"),
           "service2": ENV.get("SERVICE2_CLIENT_SECRET")}
OIDC_SECRETS = {"console": ENV.get("OIDC_CONSOLE_SECRET"), "service1": ENV.get("OIDC_SERVICE1_SECRET"),
                "service2": ENV.get("OIDC_SERVICE2_SECRET")}
for _k in ("CONSOLE_CLIENT_SECRET", "OIDC_CONSOLE_SECRET", "KEYCLOAK_ADMIN_PASSWORD"):
    if not ENV.get(_k):
        raise SystemExit(f"{_k} not found in .env — run `python scripts/generate_secrets.py` first.")


def browser():
    s = requests.Session()
    s.trust_env = False
    s.verify = CA
    return s


http = browser()
results = []


# ------------------------------------------------------------------ results
def check(name, cond, extra=""):
    results.append(bool(cond))
    print(("PASS  " if cond else "FAIL  ") + name, "" if cond else extra)


def summary():
    print(f"\n{results.count(True)}/{len(results)} checks passed")
    return 0 if all(results) else 1


# ------------------------------------------------------------------ backend API
def headers(tok=None, client="console", secret=None):
    h = {}
    if client:
        h.update({"X-Client-Id": client, "X-Client-Secret": secret or SECRETS[client]})
    if tok:
        h["Authorization"] = f"Bearer {tok}"
    return h


def _send(method, path, body=None, tok=None, client="console", secret=None, params=None):
    r = http.request(method, BASE + path, json=body, params=params, headers=headers(tok, client, secret))
    try:
        data = r.json() if r.content else {}
    except ValueError:
        data = {}
    return r.status_code, data


def get(path, tok=None, **kw):
    return _send("GET", path, None, tok, **kw)


def post(path, body=None, tok=None, **kw):
    return _send("POST", path, body or {}, tok, **kw)


def put(path, body=None, tok=None, **kw):
    return _send("PUT", path, body or {}, tok, **kw)


def delete(path, tok=None, **kw):
    return _send("DELETE", path, None, tok, **kw)


def audit_missing(it_token, actions):
    """Which of these audit actions have NOT been recorded (IT Admin view)."""
    missing = []
    for action in actions:
        _, data = get("/api/audit", it_token, params={"action": action, "page_size": 1})
        if data.get("total", 0) == 0:
            missing.append(action)
    return sorted(missing)


# ------------------------------------------------------------------ TOTP (RFC 6238, as Google Authenticator)
_last_window = {}


def totp(secret_b32, who="?"):
    """Current 6-digit code. Keycloak accepts each code once, so wait for a new
    30-second window if this account already used the current one."""
    window = int(time.time()) // 30
    if _last_window.get(who) == window:
        time.sleep(30 - time.time() % 30 + 0.5)
        window = int(time.time()) // 30
    _last_window[who] = window
    key = base64.b32decode(secret_b32.upper() + "=" * (-len(secret_b32) % 8))
    digest = hmac.new(key, struct.pack(">Q", window), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    return f"{(struct.unpack('>I', digest[offset:offset + 4])[0] & 0x7FFFFFFF) % 1_000_000:06d}"


# ------------------------------------------------------------------ Keycloak sign-in pages
def _form_action(text, form_id):
    for tag in re.findall(r"<form\b[^>]*>", text):
        if f'id="{form_id}"' in tag:
            return html.unescape(re.search(r'action="([^"]+)"', tag).group(1))
    return None


def _page_error(text):
    m = re.search(r'(?:kc-feedback-text|input-error[^"]*|pf-v5-c-helper-text__item-text|alert-error[^"]*)"[^>]*>'
                  r'\s*([^<]{3,})', text)
    return html.unescape(m.group(1).strip()) if m else "rejected"


class Account:
    """A user's current credentials as the scripts know them."""
    def __init__(self, username, password, secret=None):
        self.username, self.password, self.secret = username, password, secret


def _drive(b, r, acct, new_password, stop_at=None):
    """Submit Keycloak's forms until we leave Keycloak. Returns (final_response, steps).
    `stop_at`: a redirect URL prefix not to follow (an app's /callback)."""
    steps = []
    for _ in range(12):
        if r.is_redirect:
            loc = urllib.parse.urljoin(r.url, r.headers["Location"])
            if stop_at and loc.startswith(stop_at):
                r.headers["Location"] = loc
                return r, steps
            r = b.get(loc, allow_redirects=False)
            continue
        if not r.url.startswith(KEYCLOAK):
            return r, steps
        t = r.text
        if 'id="kc-form-login"' in t:
            if steps and steps[-1] == "login":
                return r, steps + ["ERROR: " + _page_error(t)]
            steps.append("login")
            r = b.post(_form_action(t, "kc-form-login"), allow_redirects=False,
                       data={"username": acct.username, "password": acct.password, "credentialId": ""})
        elif 'id="kc-otp-login-form"' in t:
            if steps and steps[-1] == "otp":
                return r, steps + ["ERROR: " + _page_error(t)]
            if not acct.secret:
                return r, steps + ["ERROR: one-time code asked but no authenticator known"]
            steps.append("otp")
            r = b.post(_form_action(t, "kc-otp-login-form"), allow_redirects=False,
                       data={"otp": totp(acct.secret, acct.username)})
        elif 'id="kc-totp-settings-form"' in t:
            raw = re.search(r'name="totpSecret" value="([^"]+)"', t).group(1)
            acct.secret = base64.b32encode(raw.encode()).decode()   # what the QR code / Google Authenticator holds
            steps.append("configure_totp")
            r = b.post(_form_action(t, "kc-totp-settings-form"), allow_redirects=False,
                       data={"totp": totp(acct.secret, acct.username), "totpSecret": raw, "mode": "manual",
                             "userLabel": "e2e test phone"})
        elif 'id="kc-passwd-update-form"' in t:
            if steps and steps[-1] == "update_password":
                return r, steps + ["ERROR: " + _page_error(t)]
            if not new_password:
                return r, steps + ["ERROR: new password asked but none given"]
            steps.append("update_password")
            r = b.post(_form_action(t, "kc-passwd-update-form"), allow_redirects=False,
                       data={"password-new": new_password, "password-confirm": new_password})
            acct.password = new_password
        else:
            return r, steps + ["ERROR: " + _page_error(t)]
    return r, steps + ["ERROR: too many steps"]


def tokens_for(acct, client="console", new_password=None, prompt=None, b=None):
    """Sign in to `client` through Keycloak's pages and get its tokens (authorization
    code + PKCE, exchanged by us instead of the app). Returns (tokens|None, steps)."""
    b = b or browser()
    redirect = APPS[client] + "/callback"
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    params = {"client_id": client, "response_type": "code", "scope": "openid", "redirect_uri": redirect,
              "state": secrets.token_urlsafe(8), "nonce": secrets.token_urlsafe(8),
              "code_challenge": challenge, "code_challenge_method": "S256"}
    if prompt:
        params["prompt"] = prompt
    r = b.get(OIDC + "/auth", params=params, allow_redirects=False)
    r, steps = _drive(b, r, acct, new_password, stop_at=redirect)
    if not r.is_redirect:
        return None, steps
    q = urllib.parse.parse_qs(urllib.parse.urlparse(r.headers["Location"]).query)
    if "code" not in q:
        return None, steps + [f"ERROR: {q.get('error')}"]
    tok = b.post(OIDC + "/token", data={"grant_type": "authorization_code", "code": q["code"][0],
                                        "redirect_uri": redirect, "client_id": client,
                                        "client_secret": OIDC_SECRETS[client], "code_verifier": verifier}).json()
    return (tok if "access_token" in tok else None), steps


def app_sign_in(app, acct, new_password=None, b=None):
    """Full browser round trip through an app's own /login. Returns (browser, final_response, steps)."""
    b = b or browser()
    r = b.get(APPS[app] + "/login", allow_redirects=False)
    r, steps = _drive(b, r, acct, new_password)
    return b, r, steps


def claims(jwt_token):
    import json
    part = jwt_token.split(".")[1]
    return json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))


def new_passphrase():
    words = ["amber", "canyon", "lattice", "orchard", "harbour", "violin", "saffron", "tundra", "quartz",
             "meadow", "pebble", "falcon", "lantern", "glacier", "willow", "comet"]
    return " ".join(secrets.choice(words).capitalize() if i == 0 else secrets.choice(words)
                    for i in range(4)) + f" {secrets.randbelow(90) + 10}"


# ------------------------------------------------------------------ Keycloak admin API (test set-up)
_admin_token = {"value": None, "exp": 0}


def kc(method, path, **kw):
    if time.time() > _admin_token["exp"]:
        r = http.post(f"{KEYCLOAK}/realms/master/protocol/openid-connect/token",
                      data={"grant_type": "password", "client_id": "admin-cli", "username": "kcadmin",
                            "password": ENV["KEYCLOAK_ADMIN_PASSWORD"]})
        if r.status_code != 200:
            raise SystemExit("Could not sign in to the Keycloak admin API as kcadmin "
                             "(is the stack up? KEYCLOAK_ADMIN_PASSWORD in .env?)")
        _admin_token.update(value=r.json()["access_token"], exp=time.time() + 50)
    r = http.request(method, ADMIN + path, headers={"Authorization": "Bearer " + _admin_token["value"]}, **kw)
    return r.json() if r.content and r.headers.get("content-type", "").startswith("application/json") else r


def kc_user(username):
    users = kc("GET", "/users", params={"username": username, "exact": "true"})
    return users[0] if users else None


def kc_credentials(username):
    return [c["type"] for c in kc("GET", f"/users/{kc_user(username)['id']}/credentials")]


def kc_in_staff(username):
    return any(g["name"] == "staff" for g in kc("GET", f"/users/{kc_user(username)['id']}/groups"))


def kc_sessions(username):
    return len(kc("GET", f"/users/{kc_user(username)['id']}/sessions"))


def prepare(username):
    """Known state for a demo account: no authenticator, no lock-out, random temporary
    password (Keycloak then asks for a new one at sign-in). Returns its Account."""
    user = kc_user(username)
    if not user:
        raise SystemExit(f"No Keycloak account for {username!r} — is the stack up and synced?")
    uid = user["id"]
    for cred in kc("GET", f"/users/{uid}/credentials"):
        if cred["type"] == "otp":
            kc("DELETE", f"/users/{uid}/credentials/{cred['id']}")
    kc("DELETE", f"/attack-detection/brute-force/users/{uid}")
    temp = "Tmp-" + "".join(secrets.choice(string.ascii_letters + string.digits) for _ in range(16))
    kc("PUT", f"/users/{uid}/reset-password", json={"type": "password", "value": temp, "temporary": True})
    return Account(username, temp)


def session_for(username):
    """Prepared account signed in to the console. Staff enrol TOTP on the way and then
    sign in once more with a code (a fresh enrolment is not a code-verified sign-in).
    Returns (Account, access_token)."""
    acct = prepare(username)
    tok, steps = tokens_for(acct, new_password=new_passphrase())
    if tok and acct.secret and "otp" not in claims(tok["access_token"]).get("amr", []):
        tok, more = tokens_for(acct, prompt="login")
        steps += more
    if not tok:
        raise SystemExit(f"Could not sign in as {username}: {steps}")
    return acct, tok["access_token"]
