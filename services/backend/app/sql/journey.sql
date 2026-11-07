-- Service journey analytics (CHANGE 007) — tables owned by the `analytics` container.
--
-- The analytics pipeline (services/analytics) reads request timestamps, works out how
-- long each request spent at each stage, predicts open requests, and writes the
-- results here. The backend only READS these tables (see db.provision_runtime_role)
-- and serves them to the console's "Analytics" page.
--
-- Idempotent like schema.sql: applied by the backend at every start-up.

-- One row per pipeline run. Only the latest successful run is shown; older runs are
-- pruned by the pipeline (ANALYTICS_KEEP_RUNS).
CREATE TABLE IF NOT EXISTS journey_runs (
    id               SERIAL PRIMARY KEY,
    started_at       TIMESTAMPTZ NOT NULL,
    finished_at      TIMESTAMPTZ,
    data_as_of       TIMESTAMPTZ,                -- the "now" every age/prediction is relative to
    status           TEXT NOT NULL,              -- ok | error
    error            TEXT,
    live_requests    INTEGER,
    demo_requests    INTEGER,
    events           INTEGER,
    open_requests    INTEGER,
    bottleneck_stage TEXT,
    backtest         JSONB,                      -- prediction accuracy on past requests
    settings         JSONB                       -- targets, risk threshold, method, look-back
);

-- Where requests get stuck: time per stage, per request type ('all' = every type).
CREATE TABLE IF NOT EXISTS journey_stage_stats (
    run_id        INTEGER NOT NULL REFERENCES journey_runs(id) ON DELETE CASCADE,
    request_type  TEXT NOT NULL,                 -- all | grant | return | shift | onboard
    stage         TEXT NOT NULL,                 -- pending_hr ... pending_euss, scheduled_wait
    n             INTEGER NOT NULL,              -- finished stage visits in the look-back window
    median_h      DOUBLE PRECISION,
    p75_h         DOUBLE PRECISION,
    p90_h         DOUBLE PRECISION,
    mean_h        DOUBLE PRECISION,
    total_h       DOUBLE PRECISION,
    share_pct     DOUBLE PRECISION,              -- share of all queue time (NULL for scheduled_wait)
    wip           INTEGER NOT NULL,              -- requests sitting in this stage now
    oldest_open_h DOUBLE PRECISION,
    PRIMARY KEY (run_id, request_type, stage)
);

-- One row per open request: predicted finish and risk of missing the target turnaround.
CREATE TABLE IF NOT EXISTS journey_predictions (
    run_id          INTEGER NOT NULL REFERENCES journey_runs(id) ON DELETE CASCADE,
    source          TEXT NOT NULL,               -- live (requests table) | demo (demo history)
    request_id      INTEGER NOT NULL,
    request_type    TEXT NOT NULL,
    stage           TEXT NOT NULL,
    stage_age_h     DOUBLE PRECISION NOT NULL,
    elapsed_h       DOUBLE PRECISION NOT NULL,
    target_h        DOUBLE PRECISION NOT NULL,
    remaining_p50_h DOUBLE PRECISION,
    remaining_p90_h DOUBLE PRECISION,
    eta_p50         TIMESTAMPTZ,
    eta_p90         TIMESTAMPTZ,
    risk_miss       DOUBLE PRECISION,            -- probability of finishing after the target
    flag            TEXT NOT NULL,               -- breached | at_risk | stuck | on_track
    stuck           BOOLEAN NOT NULL DEFAULT FALSE,
    history_n       INTEGER,                     -- past cases the estimate was drawn from
    PRIMARY KEY (run_id, source, request_id)
);

-- "Refresh now" from the console: the backend stamps this, the pipeline notices.
CREATE TABLE IF NOT EXISTS journey_control (
    id                   INTEGER PRIMARY KEY CHECK (id = 1),
    refresh_requested_at TIMESTAMPTZ
);
INSERT INTO journey_control (id) VALUES (1) ON CONFLICT (id) DO NOTHING;

-- Synthetic history for the demo (services/analytics/demo_history.py). Kept apart from
-- the real workflow tables so it can never grant access, appear in a queue or touch
-- the audit trail. Everything computed from it is labelled "demo".
CREATE TABLE IF NOT EXISTS journey_demo_requests (
    id             SERIAL PRIMARY KEY,
    type           TEXT NOT NULL,
    created_at     TIMESTAMPTZ NOT NULL,
    appointment_at TIMESTAMPTZ,
    seeded_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS journey_demo_events (
    id         SERIAL PRIMARY KEY,
    request_id INTEGER NOT NULL REFERENCES journey_demo_requests(id) ON DELETE CASCADE,
    action     TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL
);

CREATE INDEX IF NOT EXISTS request_events_request_idx ON request_events (request_id, created_at);
CREATE INDEX IF NOT EXISTS journey_demo_events_request_idx ON journey_demo_events (request_id, created_at);
