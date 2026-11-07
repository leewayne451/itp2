"""
Offline checks for the service journey analytics (no database needed).

    docker compose run --rm analytics python test_journey.py
    (or locally: cd services/analytics && python test_journey.py)
"""
import sys
from datetime import datetime, timedelta, timezone

import pandas as pd

import demo_history
import journey

NOW = datetime(2026, 11, 9, 4, 0, tzinfo=timezone.utc)      # a Monday, 12:00 SGT
PASSED, FAILED = [], []


def check(name, cond, detail=""):
    (PASSED if cond else FAILED).append(name)
    print(("PASS " if cond else "FAIL ") + name + (f"  ({detail})" if detail and not cond else ""))


def events_df(requests):
    """requests: list of (rid, type, appointment_at, [(action, hours_before_now), ...])"""
    rows = []
    for rid, rtype, appt, evs in requests:
        for i, (action, h) in enumerate(evs):
            rows.append({"source": "live", "request_id": rid, "type": rtype, "appointment_at": appt,
                         "action": action, "ts": NOW - timedelta(hours=h), "seq": i})
    df = pd.DataFrame(rows)
    df["ts"] = pd.to_datetime(df["ts"], utc=True)
    df["appointment_at"] = pd.to_datetime(df["appointment_at"], utc=True)
    return df


def demo_events(seed=7):
    rows = []
    for i, r in enumerate(demo_history.generate(now=NOW, seed=seed), 1):
        for j, (a, ts) in enumerate(r["events"]):
            rows.append({"source": "demo", "request_id": i, "type": r["type"], "appointment_at": r["appointment_at"],
                         "action": a, "ts": ts, "seq": j})
    df = pd.DataFrame(rows)
    df["ts"] = pd.to_datetime(df["ts"], utc=True)
    df["appointment_at"] = pd.to_datetime(df["appointment_at"], utc=True)
    return df


def test_replay():
    appt = NOW - timedelta(hours=10)
    iv = journey.build_intervals(events_df([
        (1, "grant", appt, [("submitted", 100), ("approved", 90), ("approved", 60), ("approved", 50),
                            ("processed", 40), ("appointment_booked", 30), ("provisioned", 8)]),
        (2, "return", None, [("submitted", 20), ("approved", 15)]),
        (3, "grant", None, [("submitted", 30), ("rejected", 25)]),
    ]), NOW)
    one = iv[iv["request_id"] == 1].set_index("stage")["hours"].round(3).to_dict()
    check("replay: every stage of a completed request measured",
          one == {"pending_hr": 10, "pending_director": 30, "pending_aa": 10, "pending_it": 10,
                  "pending_appointment": 10, "scheduled_wait": 20, "pending_euss": 2}, one)
    two = iv[iv["request_id"] == 2]
    check("replay: open request has one open row at its current stage",
          two["is_open"].sum() == 1 and two[two["is_open"]]["stage"].iloc[0] == "pending_director"
          and round(two[two["is_open"]]["hours"].iloc[0], 3) == 15)
    three = iv[iv["request_id"] == 3]
    check("replay: rejected request is closed, outcome rejected",
          not three["is_open"].any() and (three["outcome"] == "rejected").all())


def test_scheduled_wait_open():
    appt = NOW + timedelta(hours=20)
    iv = journey.build_intervals(events_df([
        (4, "grant", appt, [("submitted", 60), ("approved", 55), ("approved", 50), ("approved", 45),
                            ("processed", 40), ("appointment_booked", 30)]),
    ]), NOW)
    row = iv[iv["is_open"]].iloc[0]
    check("EUSS before the appointment = scheduled_wait with the appointment time kept",
          row["stage"] == "scheduled_wait" and row["scheduled_until"] == pd.Timestamp(appt))


def test_unknown_events_ignored():
    iv = journey.build_intervals(events_df([
        (5, "grant", None, [("submitted", 10), ("note_added", 8), ("approved", 5)]),
    ]), NOW)
    check("events that do not move the workflow are ignored",
          list(iv["stage"]) == ["pending_hr", "pending_director"] and round(iv["hours"].iloc[0], 3) == 5)


def test_bottleneck_found():
    iv = journey.build_intervals(demo_events(), NOW)
    stats = journey.stage_stats(iv, NOW)
    bn = journey.bottleneck(stats)
    check("demo history: the deliberately slow Director stage is found as the bottleneck", bn == "pending_director", bn)
    shares = stats[(stats["request_type"] == "all") & stats["share_pct"].notna()]["share_pct"].sum()
    check("queue-time shares add up to 100%", abs(shares - 100) < 1e-6, shares)
    sched = stats[(stats["request_type"] == "all") & (stats["stage"] == "scheduled_wait")].iloc[0]
    check("scheduled wait is reported but never counted as a queue", pd.isna(sched["share_pct"]) and sched["n"] > 0)


def test_predictions():
    iv = journey.build_intervals(demo_events(), NOW)
    s = journey.Settings()
    p = journey.predict_open(iv, NOW, s)
    check("every open request gets a prediction", len(p) == int(iv["is_open"].sum()), (len(p), iv["is_open"].sum()))
    check("p90 is never earlier than the median", bool((p["remaining_p90_h"] >= p["remaining_p50_h"]).all()))
    late = p[p["elapsed_h"] > p["target_h"]]
    check("requests already past target are flagged breached", bool((late["flag"] == "breached").all()))
    again = journey.predict_open(iv, NOW, s)
    check("predictions are reproducible run to run",
          p["remaining_p50_h"].round(6).tolist() == again["remaining_p50_h"].round(6).tolist())

    # A request that has sat at HR far longer than any past one is "stuck".
    extra = events_df([(9001, "grant", None, [("submitted", 400)])])
    iv2 = journey.build_intervals(pd.concat([demo_events(), extra], ignore_index=True), NOW)
    p2 = journey.predict_open(iv2, NOW, s).set_index("request_id")
    check("a request far beyond the usual stage time is stuck and past target",
          bool(p2.loc[9001, "stuck"]) and p2.loc[9001, "flag"] == "breached")


def test_backtest_beats_baseline():
    iv = journey.build_intervals(demo_events(), NOW)
    bt = journey.backtest(iv, journey.Settings())
    check("backtest has enough points", bt["points"] > 200, bt["points"])
    check("model error is lower than the naive baseline", bt["mae_h"] < bt["baseline_mae_h"],
          (bt["mae_h"], bt["baseline_mae_h"]))
    check("p90 estimate covers roughly 90% of outcomes", 0.75 <= bt["p90_coverage"] <= 0.97, bt["p90_coverage"])


def test_empty():
    empty = pd.DataFrame(columns=["source", "request_id", "type", "appointment_at", "action", "ts", "seq"])
    iv = journey.build_intervals(empty, NOW)
    check("no data: nothing breaks",
          iv.empty and journey.predict_open(iv, NOW, journey.Settings()).empty
          and journey.backtest(iv, journey.Settings())["points"] == 0
          and journey.bottleneck(journey.stage_stats(iv, NOW)) is None)


if __name__ == "__main__":
    for fn in [v for k, v in sorted(globals().items()) if k.startswith("test_")]:
        fn()
    print(f"\n{len(PASSED)} passed, {len(FAILED)} failed")
    sys.exit(1 if FAILED else 0)
