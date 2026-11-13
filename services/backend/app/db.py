"""Database helpers: connections, retry-on-startup, schema + seed bootstrap."""
import contextlib
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
#   ANALYTICS (ANALYTICS_DB_USER) — the `analytics` container (CHANGE 007). Reads a few
#                             timestamp columns of the workflow tables, writes only
#                             its own journey_* result tables. See provision_analytics_role.
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
ANALYTICS_DB_USER = os.environ.get("ANALYTICS_DB_USER", "sj_analytics")
ANALYTICS_DB_PASSWORD = os.environ.get("ANALYTICS_DB_PASSWORD", "")

# Service journey analytics (CHANGE 007): result tables written only by the analytics
# role. The API reads them; it may only stamp journey_control ("Refresh now").
JOURNEY_RESULT_TABLES = ("journey_runs", "journey_stage_stats", "journey_predictions",
                         "journey_demo_requests", "journey_demo_events")
# The ONLY columns of the workflow tables the analytics role can read: no names,
# justifications, notes, documents, access or audit data (data minimisation).
ANALYTICS_READ_COLUMNS = {
    "requests": ("id", "type", "status", "created_at", "appointment_at"),
    "request_events": ("id", "request_id", "action", "created_at"),
}
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


@contextlib.contextmanager
def transaction():
    """One transaction across several statements (dict rows): commit on success,
    roll back on any error. Used where a check and a write must not interleave with
    another request's, e.g. booking the last free place in an EUSS slot (CHANGE 011)."""
    conn = get_connection()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            yield cur
        conn.commit()
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
        run_sql_file("journey.sql")      # service journey analytics tables (CHANGE 007)
        for migrate in migrations:
            migrate()
        run_sql_file("seed.sql")
        provision_runtime_role()
        provision_analytics_role()
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
            # Analytics results are read-only for the API (only the pipeline writes them).
            for table in JOURNEY_RESULT_TABLES:
                cur.execute(sql.SQL("REVOKE ALL ON {} FROM {}").format(sql.Identifier(table), role))
                cur.execute(sql.SQL("GRANT SELECT ON {} TO {}").format(sql.Identifier(table), role))
            cur.execute(sql.SQL("REVOKE ALL ON journey_control FROM {}").format(role))
            cur.execute(sql.SQL("GRANT SELECT, UPDATE ON journey_control TO {}").format(role))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def provision_analytics_role():
    """Create/refresh the role the analytics container connects as (CHANGE 007):
    column-level SELECT on the two workflow tables it needs, full use of its own
    journey_* tables, nothing else."""
    if not ANALYTICS_DB_PASSWORD:
        print("[db] ANALYTICS_DB_PASSWORD not set — analytics role not provisioned "
              "(run scripts/generate_secrets.py)", flush=True)
        return
    from psycopg2 import sql
    role = sql.Identifier(ANALYTICS_DB_USER)
    conn = psycopg2.connect(**OWNER_CONFIG)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM pg_roles WHERE rolname=%s", (ANALYTICS_DB_USER,))
            if cur.fetchone():
                cur.execute(sql.SQL("ALTER ROLE {} WITH LOGIN PASSWORD %s").format(role), (ANALYTICS_DB_PASSWORD,))
            else:
                cur.execute(sql.SQL("CREATE ROLE {} WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE "
                                    "NOINHERIT NOBYPASSRLS PASSWORD %s").format(role), (ANALYTICS_DB_PASSWORD,))
            cur.execute(sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(
                sql.Identifier(OWNER_CONFIG["dbname"]), role))
            cur.execute(sql.SQL("GRANT USAGE ON SCHEMA public TO {}").format(role))
            # Start from nothing on every table, then grant exactly what the pipeline needs.
            cur.execute(sql.SQL("REVOKE ALL ON ALL TABLES IN SCHEMA public FROM {}").format(role))
            cur.execute(sql.SQL("REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM {}").format(role))
            for table, columns in ANALYTICS_READ_COLUMNS.items():
                cur.execute(sql.SQL("GRANT SELECT ({}) ON {} TO {}").format(
                    sql.SQL(", ").join(map(sql.Identifier, columns)), sql.Identifier(table), role))
            for table in JOURNEY_RESULT_TABLES:
                cur.execute(sql.SQL("GRANT SELECT, INSERT, UPDATE, DELETE ON {} TO {}").format(
                    sql.Identifier(table), role))
            cur.execute(sql.SQL("GRANT SELECT, INSERT, UPDATE ON journey_control TO {}").format(role))
            for seq in ("journey_runs_id_seq", "journey_demo_requests_id_seq", "journey_demo_events_id_seq"):
                cur.execute(sql.SQL("GRANT USAGE, SELECT ON SEQUENCE {} TO {}").format(sql.Identifier(seq), role))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
