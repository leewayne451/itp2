"""
Password helpers that remain in the backend now that Keycloak owns sign-in.

Keycloak stores and checks passwords (Argon2id), enforces the policy (length,
blocklist, not the username/email, history) and the brute-force lock-out.
The backend only:
  * generates strong TEMPORARY passwords for new accounts / help-desk resets
    (Keycloak forces the user to replace them at first sign-in), and
  * migrates any legacy plaintext column (very old volumes) to Argon2id so the
    hashes can then be moved into Keycloak by sync.py.
"""
import secrets
import unicodedata

from argon2 import PasswordHasher

from . import db

_hasher = PasswordHasher()   # Argon2id, RFC 9106 low-memory defaults (same as Keycloak's)
_TEMP_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZabcdefghjkmnpqrstuvwxyz23456789"


def hash_password(password):
    return _hasher.hash(unicodedata.normalize("NFKC", password or ""))


def generate_temporary_password():
    """~94 bits of randomness, grouped for reading aloud / typing: 'Xk7m-...'."""
    raw = "".join(secrets.choice(_TEMP_ALPHABET) for _ in range(16))
    return "-".join(raw[i:i + 4] for i in range(0, 16, 4))


def migrate_plaintext_passwords():
    """Hash any password still stored in the legacy plaintext column, force those
    accounts to choose a new password (the old one was exposed in plaintext),
    then DROP the plaintext column so it can never be read again."""
    has_col = db.query("""SELECT 1 FROM information_schema.columns
                          WHERE table_name='employees' AND column_name='password'""", fetch="one")
    if not has_col:
        return 0
    rows = db.query("SELECT id, password FROM employees WHERE password IS NOT NULL AND password_hash IS NULL")
    for r in rows:
        db.query("UPDATE employees SET password_hash=%s, must_change_password=TRUE, password=NULL WHERE id=%s",
                 (hash_password(r["password"]), r["id"]), fetch=None)
    db.query("ALTER TABLE employees DROP COLUMN IF EXISTS password", fetch=None)
    print(f"[passwords] migrated {len(rows)} plaintext password(s) to Argon2id; plaintext column dropped",
          flush=True)
    return len(rows)
