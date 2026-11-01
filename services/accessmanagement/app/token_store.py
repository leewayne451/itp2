"""
Server-side store for each signed-in browser's OIDC tokens.

The browser's cookie holds only a random handle; the access, refresh and ID
tokens stay on the server (they never reach JavaScript or the cookie, and a
stolen cookie cannot be replayed against the backend directly). Entries are
indexed by Keycloak session id so a back-channel logout from Keycloak ends the
matching console sessions immediately.

In memory, so a console restart signs users out of the console (their Keycloak
SSO session normally signs them straight back in). Use Redis if you scale out.
"""
import secrets
import threading
import time

_lock = threading.Lock()
_entries = {}          # handle -> {"access", "refresh", "id_token", "access_exp", "kc_sid", "seen"}
_by_kc_sid = {}        # Keycloak session id -> {handles}
MAX_IDLE = 8 * 3600


def put(tokens, kc_sid, access_exp):
    handle = secrets.token_urlsafe(32)
    with _lock:
        _entries[handle] = {"access": tokens["access_token"], "refresh": tokens.get("refresh_token"),
                            "id_token": tokens.get("id_token"), "access_exp": access_exp,
                            "kc_sid": kc_sid, "seen": time.time()}
        _by_kc_sid.setdefault(kc_sid, set()).add(handle)
        _prune()
    return handle


def get(handle):
    with _lock:
        e = _entries.get(handle or "")
        if e:
            e["seen"] = time.time()
        return dict(e) if e else None


def update(handle, tokens, access_exp):
    with _lock:
        e = _entries.get(handle)
        if e:
            e.update(access=tokens["access_token"], refresh=tokens.get("refresh_token", e["refresh"]),
                     id_token=tokens.get("id_token", e["id_token"]), access_exp=access_exp)


def drop(handle):
    with _lock:
        e = _entries.pop(handle or "", None)
        if e:
            _by_kc_sid.get(e["kc_sid"], set()).discard(handle)
        return e


def drop_kc_session(kc_sid):
    """Back-channel logout: end every console session tied to this Keycloak session."""
    with _lock:
        handles = _by_kc_sid.pop(kc_sid, set())
        for h in handles:
            _entries.pop(h, None)
        return len(handles)


def _prune():
    cutoff = time.time() - MAX_IDLE
    for h in [h for h, e in _entries.items() if e["seen"] < cutoff]:
        e = _entries.pop(h)
        _by_kc_sid.get(e["kc_sid"], set()).discard(h)
