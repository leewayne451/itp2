"""Database helpers: connections, retry-on-startup, schema + seed bootstrap."""
import os
import time
import pathlib

import psycopg2
import psycopg2.extras

HERE = pathlib.Path(__file__).parent

# Two database identities (least privilege):
#   OWNER   (PGUSER)        — used only at start-up to apply the schema, migrations
#                             and grants.
#   RUNTIME (APP_DB_USER)   — used for every API request. It may read/write the
#                             business tables but on audit_log it holds ONLY
#                             SELECT + INSERT, so even a bug or SQL injection in a
#                             request cannot alter or delete audit entries.
OWNER_CONFIG = {
    "host": os.environ.get("PGHOST", "postgres"),
    "port": os.environ.get("PGPORT", "5432"),
    "dbname": os.environ.get("PGDATABASE", "accessmgmt"),
    "user": os.environ.get("PGUSER", "accessmgmt"),
    "password": os.environ.get("PGPASSWORD", ""),
}
APP_DB_USER = os.environ.get("APP_DB_USER", "sj_app")
APP_DB_PASSWORD = os.environ.get("APP_DB_PASSWORD", "")
RUNTIME_CONFIG = dict(OWNER_CONFIG, user=APP_DB_USER, password=APP_DB_PASSWORD) if APP_DB_PASSWORD else OWNER_CONFIG
if not APP_DB_PASSWORD:
    print("[db] WARNING: APP_DB_PASSWORD not set — running requests as the schema OWNER "
          "(run scripts/generate_secrets.py)", flush=True)

_bootstrapping = {"on": False}


def get_connection():
    """Open a new connection. Callers are responsible for closing it.
    Start-up work runs as the owner; everything else as the restricted runtime role."""
    conn = psycopg2.connect(**(OWNER_CONFIG if _bootstrapping["on"] else RUNTIME_CONFIG))
    conn.autocommit = False
    return conn


def query(sql, params=None, fetch="all"):
    """Run a query and return dict rows (or a single row / None)."""
    conn = get_connection()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, params or ())
            result = None
            if fetch == "all":
                result = cur.fetchall()
            elif fetch == "one":
                result = cur.fetchone()
        conn.commit()
        return result
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def wait_for_db(retries=30, delay=2):
    """Block until Postgres accepts connections (docker-compose start ordering)."""
    last_err = None
    for attempt in range(1, retries + 1):
        try:
            conn = psycopg2.connect(**OWNER_CONFIG)   # the runtime role may not exist yet
            conn.close()
            print(f"[db] connected on attempt {attempt}", flush=True)
            return
        except Exception as e:  # noqa: BLE001
            last_err = e
            print(f"[db] not ready (attempt {attempt}/{retries}): {e}", flush=True)
            time.sleep(delay)
    raise RuntimeError(f"Database never became available: {last_err}")


def run_sql_file(name):
    """Execute one of the idempotent files in sql/ in a single transaction."""
    sql = (HERE / "sql" / name).read_text(encoding="utf-8")
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(sql)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db(migrations=()):
    """As the OWNER: apply schema.sql, then data migrations (e.g. hashing legacy
    plaintext passwords), then seed.sql — all idempotent — then (re)provision the
    restricted runtime role and its grants."""
    _bootstrapping["on"] = True
    try:
        run_sql_file("schema.sql")
        for migrate in migrations:
            migrate()
        run_sql_file("seed.sql")
        provision_runtime_role()
    finally:
        _bootstrapping["on"] = False
    print("[db] schema + migrations + seed applied; runtime role provisioned", flush=True)


def provision_runtime_role():
    """Create/refresh the least-privilege role the API runs as."""
    if not APP_DB_PASSWORD:
        return
    from psycopg2 import sql
    role = sql.Identifier(APP_DB_USER)
    conn = psycopg2.connect(**OWNER_CONFIG)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM pg_roles WHERE rolname=%s", (APP_DB_USER,))
            if cur.fetchone():   # keep the password in sync with .env
                cur.execute(sql.SQL("ALTER ROLE {} WITH LOGIN PASSWORD %s").format(role), (APP_DB_PASSWORD,))
            else:                # a plain role: no superuser, createdb, createrole or RLS bypass
                cur.execute(sql.SQL("CREATE ROLE {} WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE "
                                    "NOINHERIT NOBYPASSRLS PASSWORD %s").format(role), (APP_DB_PASSWORD,))
            cur.execute(sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(
                sql.Identifier(OWNER_CONFIG["dbname"]), role))
            cur.execute(sql.SQL("GRANT USAGE ON SCHEMA public TO {}").format(role))
            cur.execute(sql.SQL("GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO {}").format(role))
            cur.execute(sql.SQL("GRANT USAGE, SELECT, UPDATE ON ALL SEQUENCES IN SCHEMA public TO {}").format(role))
            # The audit trail is append-only for the application: read and add, nothing else.
            cur.execute(sql.SQL("REVOKE ALL ON audit_log FROM {}").format(role))
            cur.execute(sql.SQL("GRANT SELECT, INSERT ON audit_log TO {}").format(role))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
