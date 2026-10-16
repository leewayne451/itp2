"""Database helpers: connections, retry-on-startup, schema + seed bootstrap."""
import os
import time
import pathlib

import psycopg2
import psycopg2.extras

HERE = pathlib.Path(__file__).parent

DB_CONFIG = {
    "host": os.environ.get("PGHOST", "postgres"),
    "port": os.environ.get("PGPORT", "5432"),
    "dbname": os.environ.get("PGDATABASE", "accessmgmt"),
    "user": os.environ.get("PGUSER", "accessmgmt"),
    "password": os.environ.get("PGPASSWORD", "accessmgmt"),
}


def get_connection():
    """Open a new connection. Callers are responsible for closing it."""
    conn = psycopg2.connect(**DB_CONFIG)
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
            conn = get_connection()
            conn.close()
            print(f"[db] connected on attempt {attempt}", flush=True)
            return
        except Exception as e:  # noqa: BLE001
            last_err = e
            print(f"[db] not ready (attempt {attempt}/{retries}): {e}", flush=True)
            time.sleep(delay)
    raise RuntimeError(f"Database never became available: {last_err}")


def init_db():
    """Apply schema.sql then seed.sql (both written to be idempotent)."""
    schema = (HERE / "sql" / "schema.sql").read_text(encoding="utf-8")
    seed = (HERE / "sql" / "seed.sql").read_text(encoding="utf-8")
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(schema)
            cur.execute(seed)
        conn.commit()
        print("[db] schema + seed applied", flush=True)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
