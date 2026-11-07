"""
Service journey analytics pipeline (CHANGE 007) — the `analytics` container.

    Extract   request timestamps from the workflow tables (and the optional demo history)
    Transform replay each request's journey, measure every stage, predict open requests
              (journey.py)
    Load      write the results to the journey_* tables, which the backend serves to
              the console's "Turnaround analytics" page

It runs every ANALYTICS_INTERVAL_SECONDS, and straight away when someone presses
"Refresh now" in the console (the backend sets journey_control.refresh_requested_at).

Least privilege: the container connects as its own database role (sj_analytics,
created by the backend at start-up). That role can read ONLY these columns:
    requests:       id, type, status, created_at, appointment_at
    request_events: id, request_id, action, created_at
— no names, justifications, notes, documents, access or audit data — and can write
only the journey_* tables. It has no web port.

    python pipeline.py            # run forever (the container's command)
    python pipeline.py --once     # one run, print a summary, exit
"""
import argparse
import json
import os
import sys
import time
import traceback

import pandas as pd
import psycopg2
import psycopg2.extras

import journey

INTERVAL = int(os.environ.get("ANALYTICS_INTERVAL_SECONDS", "60"))
POLL = 5                                   # how often to look for a "refresh now"
KEEP_RUNS = int(os.environ.get("ANALYTICS_KEEP_RUNS", "50"))
LOOKBACK_DAYS = int(os.environ.get("ANALYTICS_LOOKBACK_DAYS", "180"))


def settings_from_env():
    s = journey.Settings()
    raw = os.environ.get("JOURNEY_TARGET_HOURS", "")          # e.g. "grant=168,return=120"
    for pair in raw.split(","):
        if "=" in pair:
            k, v = pair.split("=", 1)
            try:
                s.target_hours[k.strip()] = float(v)
            except ValueError:
                print(f"[analytics] ignoring bad JOURNEY_TARGET_HOURS entry {pair!r}", flush=True)
    s.risk_threshold = float(os.environ.get("JOURNEY_RISK_THRESHOLD", s.risk_threshold))
    s.method = os.environ.get("JOURNEY_METHOD", s.method)
    return s


def connect():
    password = os.environ.get("ANALYTICS_DB_PASSWORD", "")
    if len(password) < 32:
        raise RuntimeError("ANALYTICS_DB_PASSWORD is missing or too weak. Run `python scripts/generate_secrets.py`, "
                           "then `docker compose up -d --force-recreate backend analytics`.")
    return psycopg2.connect(host=os.environ.get("PGHOST", "postgres"), port=os.environ.get("PGPORT", "5432"),
                            dbname=os.environ.get("PGDATABASE", "accessmgmt"),
                            user=os.environ.get("ANALYTICS_DB_USER", "sj_analytics"), password=password,
                            options="-c timezone=UTC")


# --------------------------------------------------------------------------- #
# Extract
# --------------------------------------------------------------------------- #
LIVE_SQL = """
    SELECT 'live' AS source, r.id AS request_id, r.type, r.status, r.appointment_at,
           e.action, e.created_at AS ts, e.id AS seq
    FROM requests r JOIN request_events e ON e.request_id = r.id
"""
# Demo history is shifted so it always ends "now" (see demo_history.py).
DEMO_SQL = """
    SELECT 'demo' AS source, d.id AS request_id, d.type, NULL AS status,
           d.appointment_at + (now() - d.seeded_at) AS appointment_at,
           e.action, e.created_at + (now() - d.seeded_at) AS ts, e.id AS seq
    FROM journey_demo_requests d JOIN journey_demo_events e ON e.request_id = d.id
"""


def extract(conn):
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("SELECT now() AS now")
        now = cur.fetchone()["now"]
        cur.execute(LIVE_SQL)
        live = cur.fetchall()
        cur.execute(DEMO_SQL)
        demo = cur.fetchall()
    df = pd.DataFrame(live + demo, columns=["source", "request_id", "type", "status", "appointment_at",
                                           "action", "ts", "seq"])
    df["ts"] = pd.to_datetime(df["ts"], utc=True)
    df["appointment_at"] = pd.to_datetime(df["appointment_at"], utc=True)
    return now, df


# --------------------------------------------------------------------------- #
# Transform
# --------------------------------------------------------------------------- #
def transform(now, events, settings):
    intervals = journey.build_intervals(events, now)
    # A live request the workflow has closed is never "open", whatever the events say.
    if not intervals.empty:
        closed_live = set(events.loc[(events["source"] == "live") & events["status"].isin(journey.TERMINAL),
                                     "request_id"])
        stale = (intervals["source"] == "live") & intervals["request_id"].isin(closed_live) & intervals["is_open"]
        intervals = intervals[~stale]
    stats = journey.stage_stats(intervals, now, LOOKBACK_DAYS)
    preds = journey.predict_open(intervals, now, settings)
    bt = journey.backtest(intervals, settings)
    meta = {
        "live_requests": int(events.loc[events["source"] == "live", "request_id"].nunique()),
        "demo_requests": int(events.loc[events["source"] == "demo", "request_id"].nunique()),
        "events": int(len(events)),
        "open_requests": int(len(preds)),
        "bottleneck_stage": journey.bottleneck(stats),
        "backtest": bt,
    }
    return stats, preds, meta


# --------------------------------------------------------------------------- #
# Load
# --------------------------------------------------------------------------- #
def _num(v):
    return None if v is None or (isinstance(v, float) and v != v) else v


def load(conn, started, now, stats, preds, meta, settings):
    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO journey_runs (started_at, finished_at, data_as_of, status, live_requests, demo_requests,
                                         events, open_requests, bottleneck_stage, backtest, settings)
               VALUES (%s, now(), %s, 'ok', %s, %s, %s, %s, %s, %s, %s) RETURNING id""",
            (started, now, meta["live_requests"], meta["demo_requests"], meta["events"], meta["open_requests"],
             meta["bottleneck_stage"], json.dumps(meta["backtest"]),
             json.dumps({"target_hours": settings.target_hours, "risk_threshold": settings.risk_threshold,
                         "method": settings.method, "lookback_days": LOOKBACK_DAYS})))
        run_id = cur.fetchone()[0]
        psycopg2.extras.execute_values(cur, """
            INSERT INTO journey_stage_stats (run_id, request_type, stage, n, median_h, p75_h, p90_h, mean_h,
                                             total_h, share_pct, wip, oldest_open_h) VALUES %s""",
            [(run_id, r.request_type, r.stage, int(r.n), _num(r.median_h), _num(r.p75_h), _num(r.p90_h),
              _num(r.mean_h), _num(r.total_h), _num(r.share_pct), int(r.wip), _num(r.oldest_open_h))
             for r in stats.itertuples(index=False)])
        if not preds.empty:
            psycopg2.extras.execute_values(cur, """
                INSERT INTO journey_predictions (run_id, source, request_id, request_type, stage, stage_age_h,
                    elapsed_h, target_h, remaining_p50_h, remaining_p90_h, eta_p50, eta_p90, risk_miss, flag,
                    stuck, history_n) VALUES %s""",
                [(run_id, r.source, int(r.request_id), r.request_type, r.stage, r.stage_age_h, r.elapsed_h,
                  r.target_h, r.remaining_p50_h, r.remaining_p90_h, r.eta_p50, r.eta_p90, r.risk_miss, r.flag,
                  bool(r.stuck), int(r.history_n)) for r in preds.itertuples(index=False)])
        # Keep a short history of runs; older ones (and their rows) go.
        cur.execute("DELETE FROM journey_runs WHERE id NOT IN "
                    "(SELECT id FROM journey_runs ORDER BY id DESC LIMIT %s)", (KEEP_RUNS,))
    conn.commit()
    return run_id


def record_failure(conn, started, err):
    try:
        conn.rollback()
        with conn.cursor() as cur:
            cur.execute("INSERT INTO journey_runs (started_at, finished_at, status, error) "
                        "VALUES (%s, now(), 'error', %s)", (started, err[:2000]))
        conn.commit()
    except Exception:  # noqa: BLE001 — the database itself may be what failed
        pass


def run_once(conn, settings, verbose=False):
    with conn.cursor() as cur:
        cur.execute("SELECT now()")
        started = cur.fetchone()[0]
    try:
        now, events = extract(conn)
        conn.commit()                       # end the read transaction
        stats, preds, meta = transform(now, events, settings)
        run_id = load(conn, started, now, stats, preds, meta, settings)
    except Exception as e:  # noqa: BLE001
        record_failure(conn, started, f"{type(e).__name__}: {e}")
        raise
    bt = meta["backtest"]
    print(f"[analytics] run #{run_id}: {meta['live_requests']} live + {meta['demo_requests']} demo requests, "
          f"{meta['open_requests']} open, bottleneck={meta['bottleneck_stage']}, "
          f"backtest MAE={bt['mae_h'] and round(bt['mae_h'], 1)}h (baseline "
          f"{bt['baseline_mae_h'] and round(bt['baseline_mae_h'], 1)}h)", flush=True)
    if verbose:
        flags = preds["flag"].value_counts().to_dict() if not preds.empty else {}
        print(json.dumps({"flags": flags, "backtest": bt}, indent=2, default=str))
    return run_id


def _refresh_requested(conn, last_started):
    with conn.cursor() as cur:
        cur.execute("SELECT refresh_requested_at FROM journey_control WHERE id = 1")
        row = cur.fetchone()
    conn.commit()
    return bool(row and row[0] and (last_started is None or row[0] > last_started))


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true", help="run once and exit")
    args = ap.parse_args(argv)
    settings = settings_from_env()
    if args.once:
        conn = connect()
        try:
            run_once(conn, settings, verbose=True)
        finally:
            conn.close()
        return 0

    print(f"[analytics] starting: every {INTERVAL}s, targets {settings.target_hours} h, "
          f"method {settings.method}", flush=True)
    conn, last_started, next_due = None, None, 0.0
    while True:
        try:
            if conn is None or conn.closed:
                conn = connect()
            if time.monotonic() >= next_due or _refresh_requested(conn, last_started):
                with conn.cursor() as cur:
                    cur.execute("SELECT now()")
                    last_started = cur.fetchone()[0]
                conn.commit()
                run_once(conn, settings)
                next_due = time.monotonic() + INTERVAL
        except psycopg2.OperationalError as e:
            # Database not up yet / restarted, or the role is not provisioned yet (the
            # backend creates it at start-up): try again shortly.
            print(f"[analytics] database unavailable ({str(e).strip().splitlines()[0]}); retrying", flush=True)
            conn = None
            next_due = 0.0
        except Exception:  # noqa: BLE001 — one bad run must not stop the service
            traceback.print_exc()
            next_due = time.monotonic() + INTERVAL
        time.sleep(POLL)


if __name__ == "__main__":
    sys.exit(main())
