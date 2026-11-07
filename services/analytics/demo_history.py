"""
Synthetic request history for the demo, so the bottleneck chart and the predictions
have something to learn from. A fresh POC database has only the handful of requests
raised during testing, and real requests take days to move through five teams.

    docker compose exec analytics python demo_history.py seed     # replace demo history
    docker compose exec analytics python demo_history.py purge    # remove it
    docker compose exec analytics python demo_history.py status

It is written ONLY to the analytics-owned tables journey_demo_requests /
journey_demo_events. The real workflow tables (requests, request_events,
employee_access, assets, audit_log) are never touched, so the demo history cannot
grant anyone access, appear in a dashboard queue, or alter the audit trail. Every
result computed from it is labelled "demo" in the console.

The pipeline shifts demo timestamps forward by the time elapsed since seeding, so the
history always "ends now": open demo requests keep the same age however long ago you
seeded. Re-seed with a different --seed to get a different history.

Shape of the generated history (deliberately realistic, with a visible bottleneck):
  * ~2 requests per working day over the last --days days, submitted in office hours
  * approvals and processing happen in office hours (Mon-Fri 09:00-18:00 SGT), so a
    request that reaches a desk on Friday evening waits for the weekend
  * the Director stage is the slowest approval, and slows down further over the last
    six weeks (a growing backlog the chart should reveal)
  * ~7% of requests are rejected at one of the approval stages, and ~5% stall at one
    desk for several days
"""
import argparse
import math
import random
import sys
from datetime import datetime, timedelta, timezone

SGT = timezone(timedelta(hours=8))
TYPE_WEIGHTS = {"grant": 0.5, "return": 0.2, "shift": 0.15, "onboard": 0.15}
# (median hours, log-normal sigma) of the time each stage takes, before office-hours rounding
STAGE = {
    "hr": (2.5, 0.8),
    "director": (8.0, 0.9),
    "director_backlog": (22.0, 0.9),    # last six weeks
    "aa": (3.5, 0.8),
    "it": {"grant": (3.0, 0.7), "return": (2.0, 0.6), "shift": (6.0, 0.7), "onboard": (6.0, 0.7)},
    "book": (2.5, 1.0),                  # requestor books an appointment
    "lead": (22.0, 0.6),                 # booking -> appointment date
    "euss": (1.5, 0.6),                  # appointment -> EUSS completes it
}
REJECT_P = {"hr": 0.02, "director": 0.03, "aa": 0.02}
# A few requests stall at one desk for days (someone on leave, a missing document).
STALL_P = 0.05
STALL_HOURS = (80.0, 0.4)


def _lognormal(rng, median, sigma):
    return rng.lognormvariate(math.log(median), sigma)


def _office(ts, rng):
    """Move a timestamp into office hours (Mon-Fri 09:00-18:00 SGT), as a person would."""
    local = ts.astimezone(SGT)
    while True:
        if local.weekday() >= 5:                      # weekend -> Monday morning
            local = (local + timedelta(days=7 - local.weekday())).replace(hour=9, minute=0)
            local += timedelta(minutes=rng.randint(0, 90))
            continue
        if local.hour < 9:
            local = local.replace(hour=9, minute=0) + timedelta(minutes=rng.randint(0, 60))
            continue
        if local.hour >= 18:                          # after hours -> next morning
            local = (local + timedelta(days=1)).replace(hour=9, minute=0) + timedelta(minutes=rng.randint(0, 60))
            continue
        return local.astimezone(timezone.utc)


def generate(now=None, days=120, seed=7):
    """Return a list of {type, created_at, appointment_at, events: [(action, ts)]};
    events after `now` are cut off, which leaves the latest requests open mid-workflow."""
    rng = random.Random(seed)
    now = now or datetime.now(timezone.utc)
    start = now - timedelta(days=days)
    backlog_from = now - timedelta(days=42)
    out = []
    day = start
    while day < now:
        if day.astimezone(SGT).weekday() < 5:
            for _ in range(rng.choices([0, 1, 2, 3, 4], [0.1, 0.25, 0.3, 0.25, 0.1])[0]):
                t0 = _office(day.astimezone(SGT).replace(hour=9, minute=0).astimezone(timezone.utc)
                             + timedelta(minutes=rng.randint(0, 540)), rng)
                if t0 < now:
                    out.append(_one_request(rng, t0, backlog_from, now))
        day += timedelta(days=1)
    return out


def _one_request(rng, t0, backlog_from, now):
    rtype = rng.choices(list(TYPE_WEIGHTS), list(TYPE_WEIGHTS.values()))[0]
    events = [("submitted", t0)]
    t = t0
    stall_at = rng.choice(["hr", "director", "aa", "it"]) if rng.random() < STALL_P else None

    def extra(stage):
        return _lognormal(rng, *STALL_HOURS) if stage == stall_at else 0.0

    for stage in ("hr", "director", "aa"):
        params = STAGE["director_backlog"] if (stage == "director" and t >= backlog_from) else STAGE[stage]
        t = _office(t + timedelta(hours=_lognormal(rng, *params) + extra(stage)), rng)
        if rng.random() < REJECT_P[stage]:
            events.append(("rejected", t))
            return _cut(rtype, t0, None, events, now)
        events.append(("approved", t))
    t = _office(t + timedelta(hours=_lognormal(rng, *STAGE["it"][rtype]) + extra("it")), rng)
    events.append(("processed", t))
    t = _office(t + timedelta(hours=_lognormal(rng, *STAGE["book"])), rng)
    events.append(("appointment_booked", t))
    appt = _office(t + timedelta(hours=_lognormal(rng, *STAGE["lead"])), rng)
    appt = appt.replace(minute=(appt.minute // 30) * 30, second=0, microsecond=0)   # slots on the half hour
    done = _office(appt + timedelta(hours=_lognormal(rng, *STAGE["euss"])), rng)
    events.append(("retrieved" if rtype == "return" else "provisioned", max(done, appt)))
    return _cut(rtype, t0, appt, events, now)


def _cut(rtype, t0, appt, events, now):
    kept = [(a, ts) for a, ts in events if ts <= now]
    booked = any(a == "appointment_booked" for a, _ in kept)
    return {"type": rtype, "created_at": t0, "appointment_at": appt if booked else None, "events": kept}


# --------------------------------------------------------------------------- #
# Database
# --------------------------------------------------------------------------- #
def seed(conn, days, seed_value):
    history = generate(days=days, seed=seed_value)
    with conn.cursor() as cur:
        cur.execute("DELETE FROM journey_demo_requests")
        cur.execute("SELECT now()")
        seeded_at = cur.fetchone()[0]
        for r in history:
            cur.execute("INSERT INTO journey_demo_requests (type, created_at, appointment_at, seeded_at) "
                        "VALUES (%s,%s,%s,%s) RETURNING id",
                        (r["type"], r["created_at"], r["appointment_at"], seeded_at))
            rid = cur.fetchone()[0]
            cur.executemany("INSERT INTO journey_demo_events (request_id, action, created_at) VALUES (%s,%s,%s)",
                            [(rid, a, ts) for a, ts in r["events"]])
        cur.execute("INSERT INTO journey_control (id, refresh_requested_at) VALUES (1, now()) "
                    "ON CONFLICT (id) DO UPDATE SET refresh_requested_at = now()")
    conn.commit()
    open_n = sum(1 for r in history if r["events"][-1][0] not in ("provisioned", "retrieved", "rejected"))
    print(f"[demo] seeded {len(history)} demo requests over {days} days ({open_n} still open); "
          f"the pipeline will pick them up within a few seconds.")


def purge(conn):
    with conn.cursor() as cur:
        cur.execute("DELETE FROM journey_demo_requests")
        n = cur.rowcount
        cur.execute("INSERT INTO journey_control (id, refresh_requested_at) VALUES (1, now()) "
                    "ON CONFLICT (id) DO UPDATE SET refresh_requested_at = now()")
    conn.commit()
    print(f"[demo] removed {n} demo requests.")


def status(conn):
    with conn.cursor() as cur:
        cur.execute("SELECT count(*), min(seeded_at) FROM journey_demo_requests")
        n, when = cur.fetchone()
    print(f"[demo] {n} demo requests" + (f", seeded {when.astimezone(SGT):%Y-%m-%d %H:%M} SGT" if n else ""))


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[1])
    ap.add_argument("command", choices=["seed", "purge", "status"])
    ap.add_argument("--days", type=int, default=120, help="how far back the history goes (default 120)")
    ap.add_argument("--seed", type=int, default=7, help="random seed: same seed = same history (default 7)")
    args = ap.parse_args(argv)
    from pipeline import connect                      # same least-privilege connection as the pipeline
    conn = connect()
    try:
        {"seed": lambda: seed(conn, args.days, args.seed), "purge": lambda: purge(conn),
         "status": lambda: status(conn)}[args.command]()
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
