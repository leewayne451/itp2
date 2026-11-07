# Service journey analytics (CHANGE 007)

Shows where access requests actually get stuck, flags open requests at risk of missing
their target turnaround, and predicts when each will finish. Built only from the
timestamps the workflow already writes to `request_events`.

## How it fits

```
requests / request_events ──(read: ids, types, statuses, timestamps only)──▶ analytics container
                                                                              Pandas ETL, every 60 s
journey_* tables ◀──(write: stage stats, predictions, backtest)───────────────┘
      │
      └──(read-only)──▶ backend GET /api/journey ──▶ console "Analytics" page
                         (adds names for live requests)   (HR, Director, AA, IT Admin, EUSS, Admin)
```

* **Own database role** `sj_analytics`, created by the backend at start-up: column-level
  `SELECT` on `requests(id, type, status, created_at, appointment_at)` and
  `request_events(id, request_id, action, created_at)`, full use of the `journey_*` tables,
  nothing else. It cannot read names, justifications, notes, documents, access or the audit
  trail. The API role `sj_app` can only read the results (plus stamp "Refresh now").
* **No web port**, runs as a non-root user.

## What it computes

| | |
|---|---|
| **Stage times** | Replays each request's events through the workflow (HR → Director → AA → IT → requestor books → EUSS) to get time spent per stage. The EUSS stage is split into *waiting for the appointment date* (planned, never called a bottleneck) and the hand-over itself. |
| **Bottleneck** | The stage with the largest share of all queue time over the look-back window (180 days). Shown overall and per request type. |
| **Prediction** | For an open request at stage S for *a* hours: take past requests of the same type that spent more than *a* hours at S and see how much longer they took to complete (falls back to adding up sampled stage times when fewer than 8 such cases). 4,000 Monte Carlo samples give the median finish, a 90th-percentile "latest", and P(miss target). |
| **Flags** | `breached` (past target) · `at_risk` (P(miss) ≥ 50%) · `stuck` (at the current stage longer than 90% of past requests) · `on_track` |
| **Backtest** | Walk-forward replay on finished requests using only history available at each moment. Reports mean absolute error vs a naive baseline, bias, 90th-percentile coverage, and precision / recall of the at-risk flag. |

Durations are calendar hours. Targets (submission → completed) default to grant 7 days,
return 7 days, shift 8 days, onboarding 8 days.

## Settings (docker-compose / .env)

| Variable | Default | |
|---|---|---|
| `JOURNEY_TARGET_HOURS` | `grant=168,return=168,shift=192,onboard=192` | target turnaround per type |
| `JOURNEY_RISK_THRESHOLD` | `0.5` | P(miss) at which a request is "at risk" |
| `ANALYTICS_INTERVAL_SECONDS` | `60` | how often it recomputes |
| `ANALYTICS_DB_PASSWORD` | from `scripts/generate_secrets.py` | role password (required) |

## Demo

A fresh POC database has only a few requests, so seed synthetic history (written only to
`journey_demo_*`, never to the real workflow tables, and labelled **demo** in the console):

```
python scripts/generate_secrets.py                 # adds ANALYTICS_DB_PASSWORD to .env
docker compose up -d --build backend analytics accessmanagement
docker compose exec analytics python demo_history.py seed
docker compose exec analytics python test_journey.py     # 17 offline checks
```

Then sign in as `director` (or HR / AA / IT Admin / EUSS / Admin) → **Analytics**.
The seeded history has a slow Director stage that got slower over the last six weeks, so
it shows up as the bottleneck. Raise a real request as `req`, approve a step or two, press
**Refresh now**: it appears in the predictions with a link to its details.

`demo_history.py purge` removes the demo history; `status` shows what is loaded. Demo
timestamps are shifted to always end "now", so seeding the day before a presentation is fine.

## Limits (POC)

* Calendar time, not working hours; a request is predicted as if it will complete
  (rejections are not modelled).
* A sudden stall (someone on leave) cannot be foreseen, so recall of the at-risk flag is
  modest; it is caught by the `stuck` flag once it happens.
* Accuracy figures on the demo history describe the method, not real MND turnaround data.
