"""
Service journey analytics: the "transform" step of the pipeline (pure functions,
no database), so it can be unit-tested on its own (see test_journey.py).

Input: one row per workflow event (request_events), with the request's type and
appointment time. From the order of those events we replay the workflow and work
out how long each request spent in each stage, then:

  * stage_stats()  — where requests actually get stuck (median / p90 time per stage,
                     share of all waiting time, how many are sitting there now)
  * predict_open() — for every open request: when it will probably finish, the chance
                     it misses its target turnaround, and whether it is "stuck"
  * backtest()     — how good those predictions would have been on past requests

Prediction method (kept deliberately simple and explainable for the POC):
Monte Carlo over the EMPIRICAL stage durations of past requests. For a request that
has been in stage S for `a` hours we sample "how much longer S takes, given it has
already taken a" from past S durations longer than a, then add one sampled duration
for every stage still ahead of it. Repeating this a few thousand times gives a
distribution of the finish time: its median is the estimate, its 90th percentile a
pessimistic bound, and the share of samples past the target is the risk of missing it.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

import numpy as np
import pandas as pd

# --------------------------------------------------------------------------- #
# The workflow (mirrors STATE_ROLE / APPROVAL_NEXT in services/backend/app/core.py)
# --------------------------------------------------------------------------- #
APPROVAL_NEXT = {
    "pending_hr": "pending_director",
    "pending_director": "pending_aa",
    "pending_aa": "pending_it",
}
# Order in which a request passes through the stages. "scheduled_wait" is the time
# between booking an appointment and the appointment itself: it is waiting by design,
# not a queue anyone can clear, so it is reported but never named the bottleneck.
STAGES = [
    "pending_hr", "pending_director", "pending_aa", "pending_it",
    "pending_appointment", "scheduled_wait", "pending_euss",
]
NOT_A_QUEUE = {"scheduled_wait"}
STAGE_OWNER = {
    "pending_hr": "HR", "pending_director": "Director", "pending_aa": "Approving Authority",
    "pending_it": "IT Admin", "pending_appointment": "Requestor",
    "scheduled_wait": "(appointment date)", "pending_euss": "EUSS",
}
TERMINAL = {"completed", "rejected"}
REQUEST_TYPES = ["grant", "return", "shift", "onboard"]


def next_state(state: str | None, action: str) -> str | None:
    """Workflow state after `action`, or None if the action does not move the request."""
    if action == "submitted":
        return "pending_hr"
    if action == "approved":
        return APPROVAL_NEXT.get(state)
    if action == "processed":
        return "pending_appointment"
    if action == "appointment_booked":
        return "pending_euss"
    if action in ("provisioned", "retrieved"):
        return "completed"
    if action == "rejected":
        return "rejected"
    return None


def _hours(td) -> float:
    return td.total_seconds() / 3600.0


# --------------------------------------------------------------------------- #
# 1. Replay: events -> one row per (request, stage) interval
# --------------------------------------------------------------------------- #
INTERVAL_COLUMNS = ["source", "request_id", "type", "stage", "start", "end", "hours", "is_open",
                    "scheduled_until", "submitted_at", "outcome"]


def build_intervals(events: pd.DataFrame, now: datetime) -> pd.DataFrame:
    """events columns: source, request_id, type, appointment_at, action, ts (tz-aware), seq.

    Returns one row per stage visit. Closed rows have `end` and `hours` = duration;
    the open row of an unfinished request has end=NaT and `hours` = age so far.
    `outcome` (completed / rejected / open) is the request's final state, on every row.
    """
    rows = []
    if events.empty:
        return pd.DataFrame(columns=INTERVAL_COLUMNS)
    ev = events.sort_values(["source", "request_id", "ts", "seq"])
    for (source, rid), g in ev.groupby(["source", "request_id"], sort=False):
        rtype = g["type"].iloc[0]
        appt = g["appointment_at"].iloc[0]
        appt = None if pd.isna(appt) else appt.to_pydatetime()
        state, since, submitted = None, None, None
        visits = []
        for action, ts in zip(g["action"], g["ts"]):
            ts = ts.to_pydatetime()
            new = next_state(state, action)
            if action == "submitted" and submitted is None:
                submitted = ts
            if new is None or new == state:
                continue                       # an event that does not move the request
            if state is not None:
                visits.append((state, since, ts))
            state, since = new, ts
        if submitted is None:
            continue                           # never submitted: nothing to measure
        outcome = state if state in TERMINAL else "open"
        if state not in TERMINAL and state is not None:
            visits.append((state, since, None))
        for stage, start, end in visits:
            rows.extend(_split_euss(source, rid, rtype, stage, start, end, appt, now, submitted, outcome))
    df = pd.DataFrame(rows, columns=INTERVAL_COLUMNS)
    if not df.empty:
        for col in ("start", "end", "scheduled_until", "submitted_at"):
            df[col] = pd.to_datetime(df[col], utc=True)
    return df


def _split_euss(source, rid, rtype, stage, start, end, appt, now, submitted, outcome):
    """The EUSS stage starts when the appointment is booked, but the time up to the
    appointment itself is planned waiting. Split it into scheduled_wait + pending_euss."""
    def row(st, a, b, scheduled_until=None):
        is_open = b is None
        hours = _hours((now if is_open else b) - a)
        return [source, rid, rtype, st, a, b, max(hours, 0.0), is_open, scheduled_until, submitted, outcome]

    if stage != "pending_euss" or appt is None or appt <= start:
        return [row(stage, start, end)]
    if end is not None and end <= appt:        # done before the appointment time
        return [row("scheduled_wait", start, end)]
    if end is None and now < appt:             # still waiting for the appointment date
        return [row("scheduled_wait", start, None, scheduled_until=appt)]
    return [row("scheduled_wait", start, appt), row("pending_euss", appt, end)]


# --------------------------------------------------------------------------- #
# 2. Where requests get stuck
# --------------------------------------------------------------------------- #
def stage_stats(intervals: pd.DataFrame, now: datetime, lookback_days: int = 180) -> pd.DataFrame:
    """One row per (request_type, stage); request_type 'all' aggregates every type.
    Durations come from stage visits that ENDED inside the look-back window;
    wip / oldest_open_h describe requests sitting in the stage right now."""
    out = []
    if intervals.empty:
        return pd.DataFrame(columns=["request_type", "stage", "n", "median_h", "p75_h", "p90_h", "mean_h",
                                     "total_h", "share_pct", "wip", "oldest_open_h"])
    since = pd.Timestamp(now) - pd.Timedelta(days=lookback_days)
    closed = intervals[(~intervals["is_open"]) & (intervals["end"] >= since)]
    open_ = intervals[intervals["is_open"]]
    for rtype in ["all"] + REQUEST_TYPES:
        c = closed if rtype == "all" else closed[closed["type"] == rtype]
        o = open_ if rtype == "all" else open_[open_["type"] == rtype]
        queue_total = c[~c["stage"].isin(NOT_A_QUEUE)]["hours"].sum()
        for stage in STAGES:
            d = c.loc[c["stage"] == stage, "hours"]
            w = o.loc[o["stage"] == stage, "hours"]
            total = float(d.sum())
            out.append({
                "request_type": rtype, "stage": stage, "n": int(d.size),
                "median_h": float(d.median()) if d.size else None,
                "p75_h": float(d.quantile(.75)) if d.size else None,
                "p90_h": float(d.quantile(.90)) if d.size else None,
                "mean_h": float(d.mean()) if d.size else None,
                "total_h": total,
                "share_pct": (100.0 * total / queue_total) if (queue_total and stage not in NOT_A_QUEUE) else None,
                "wip": int(w.size),
                "oldest_open_h": float(w.max()) if w.size else None,
            })
    return pd.DataFrame(out)


def bottleneck(stats: pd.DataFrame, request_type: str = "all") -> str | None:
    """The stage that accounts for the largest share of all queue time."""
    s = stats[(stats["request_type"] == request_type) & stats["share_pct"].notna() & (stats["n"] > 0)]
    if s.empty:
        return None
    return s.sort_values(["share_pct", "wip"], ascending=False).iloc[0]["stage"]


# --------------------------------------------------------------------------- #
# 3. Prediction
# --------------------------------------------------------------------------- #
@dataclass
class Settings:
    # Target turnaround, submission -> completed, in calendar hours (override: JOURNEY_TARGET_HOURS)
    target_hours: dict = field(default_factory=lambda: {"grant": 168, "return": 168, "shift": 192, "onboard": 192})
    risk_threshold: float = 0.5        # flag "at risk" when P(miss target) >= this
    min_samples: int = 5               # below this, a type-specific pool falls back to all types
    min_analogs: int = 8               # fewer comparable past journeys -> use the stage-sum estimate
    method: str = "analog"             # "analog" or "stage_sum"
    draws: int = 4000                  # Monte Carlo samples per live prediction
    backtest_draws: int = 400
    min_history: int = 10              # completed requests needed before backtesting a point


def _ns(ts):
    """tz-aware timestamp -> naive UTC numpy datetime64 (how the pools store times)."""
    t = pd.Timestamp(ts)
    return (t.tz_convert(None) if t.tzinfo else t).to_datetime64()


class Predictor:
    """Learns from finished history, optionally only from what had finished before a
    cut-off time (so a backtest never peeks at the future). Two estimators:

    * analog (preferred): "past requests of this type that sat in this stage at least
      as long as this one has — how long did they still take to complete?" Using whole
      past journeys keeps real-world effects that hit several stages at once (weekends,
      a backlog period) instead of assuming every stage is independent.
    * stage-sum (fallback when there are too few analogs): rest of the current stage
      plus one sampled duration per stage still ahead.
    """

    def __init__(self, closed: pd.DataFrame, settings: Settings):
        self.s = settings
        self._by_type, self._all = {}, {}
        self._paths_by_type, self._paths_all = {}, {}
        c = closed.sort_values("end")
        for stage, g in c.groupby("stage"):
            self._all[stage] = (g["end"].to_numpy(dtype="datetime64[ns]"), g["hours"].to_numpy(float))
        for (rtype, stage), g in c.groupby(["type", "stage"]):
            self._by_type[(rtype, stage)] = (g["end"].to_numpy(dtype="datetime64[ns]"), g["hours"].to_numpy(float))

        # Whole journeys of COMPLETED requests: for each stage visit, its duration and the
        # time from entering that stage to completion. Ordered by completion time.
        done = c[c["outcome"] == "completed"].copy()
        if not done.empty:
            done["finish"] = done.groupby(["source", "request_id"])["end"].transform("max")
            done["to_finish_h"] = (done["finish"] - done["start"]).dt.total_seconds() / 3600
            done = done.sort_values("finish")

            def pack(g):
                return (g["finish"].to_numpy(dtype="datetime64[ns]"), g["hours"].to_numpy(float),
                        g["to_finish_h"].to_numpy(float))
            for stage, g in done.groupby("stage"):
                self._paths_all[stage] = pack(g)
            for (rtype, stage), g in done.groupby(["type", "stage"]):
                self._paths_by_type[(rtype, stage)] = pack(g)

    @staticmethod
    def _cut(arrays, cutoff):
        if cutoff is None:
            return arrays[1:]
        k = np.searchsorted(arrays[0], _ns(cutoff), side="left")
        return tuple(a[:k] for a in arrays[1:])

    def pool(self, rtype, stage, cutoff=None) -> np.ndarray:
        if (rtype, stage) in self._by_type:
            (d,) = self._cut(self._by_type[(rtype, stage)], cutoff)
            if d.size >= self.s.min_samples:
                return d
        if stage in self._all:
            return self._cut(self._all[stage], cutoff)[0]
        return np.empty(0)

    def analogs(self, rtype, stage, age_h, cutoff=None) -> np.ndarray:
        """Remaining hours of past journeys that had spent > age_h in this stage."""
        for key, table in (((rtype, stage), self._paths_by_type), (stage, self._paths_all)):
            if key in table:
                dur, to_finish = self._cut(table[key], cutoff)
                keep = dur > age_h if age_h > 0 else np.ones(dur.size, bool)
                if keep.sum() >= self.s.min_analogs:
                    return to_finish[keep] - age_h
        return np.empty(0)

    def sample_remaining(self, rtype, stage, age_h, rng, draws, cutoff=None, scheduled_until_h=None):
        """Samples of the hours still to go until the request is completed, and the
        number of past cases they were drawn from."""
        if not (stage == "scheduled_wait" and scheduled_until_h is not None) and self.s.method == "analog":
            a = self.analogs(rtype, stage, age_h, cutoff)
            if a.size:
                return rng.choice(a, draws), int(a.size)
        return self._stage_sum(rtype, stage, age_h, rng, draws, cutoff, scheduled_until_h)

    def _stage_sum(self, rtype, stage, age_h, rng, draws, cutoff, scheduled_until_h):
        total = np.zeros(draws)
        smallest_pool = None

        # (a) rest of the CURRENT stage, given it has already lasted age_h
        if stage == "scheduled_wait" and scheduled_until_h is not None:
            total += max(scheduled_until_h, 0.0)          # the appointment time is known
        else:
            p = self.pool(rtype, stage, cutoff)
            smallest_pool = p.size
            if p.size:
                resid = p[p > age_h] - age_h
                if resid.size < 3:
                    # Older than (nearly) everything seen before: assume it behaves like
                    # the slow tail — the residual life of visits beyond the 75th percentile.
                    q = np.quantile(p, .75)
                    resid = p[p > q] - q
                if resid.size == 0:
                    resid = np.array([float(np.median(p))])
                total += rng.choice(resid, draws)

        # (b) one sampled duration for each stage still ahead
        for nxt in STAGES[STAGES.index(stage) + 1:]:
            p = self.pool(rtype, nxt, cutoff)
            smallest_pool = p.size if smallest_pool is None else min(smallest_pool, p.size)
            if p.size:
                total += rng.choice(p, draws)
        return total, (smallest_pool or 0)

    def stage_p90(self, rtype, stage, cutoff=None):
        p = self.pool(rtype, stage, cutoff)
        return float(np.quantile(p, .9)) if p.size >= self.s.min_samples else None


def _rng(source, rid, stage):
    key = f"{source}:{rid}:{stage}"
    return np.random.default_rng(abs(hash_stable(key)) % (2 ** 32))


def hash_stable(text: str) -> int:
    """Deterministic across processes (Python's hash() is salted per process)."""
    h = 2166136261
    for ch in text.encode():
        h = ((h ^ ch) * 16777619) & 0xFFFFFFFF
    return h


def predict_open(intervals: pd.DataFrame, now: datetime, settings: Settings) -> pd.DataFrame:
    """One row per open request: finish-time estimate, risk of missing the target, flags."""
    cols = ["source", "request_id", "request_type", "stage", "stage_age_h", "elapsed_h", "target_h",
            "remaining_p50_h", "remaining_p90_h", "eta_p50", "eta_p90", "risk_miss", "flag", "stuck", "history_n"]
    if intervals.empty:
        return pd.DataFrame(columns=cols)
    pred = Predictor(intervals[~intervals["is_open"]], settings)
    out = []
    for r in intervals[intervals["is_open"]].itertuples(index=False):
        sched = None
        if r.stage == "scheduled_wait" and not pd.isna(r.scheduled_until):
            sched = _hours(r.scheduled_until.to_pydatetime() - now)
        samples, n = pred.sample_remaining(r.type, r.stage, r.hours, _rng(r.source, r.request_id, r.stage),
                                           settings.draws, scheduled_until_h=sched)
        elapsed = _hours(now - r.submitted_at.to_pydatetime())
        target = float(settings.target_hours.get(r.type, 168))
        p50, p90 = float(np.median(samples)), float(np.quantile(samples, .9))
        risk = float(np.mean(elapsed + samples > target))
        p90_stage = pred.stage_p90(r.type, r.stage)
        stuck = bool(r.stage not in NOT_A_QUEUE and p90_stage is not None and r.hours > p90_stage)
        if elapsed > target:
            flag, risk = "breached", 1.0
        elif risk >= settings.risk_threshold:
            flag = "at_risk"
        elif stuck:
            flag = "stuck"
        else:
            flag = "on_track"
        out.append({
            "source": r.source, "request_id": int(r.request_id), "request_type": r.type, "stage": r.stage,
            "stage_age_h": float(r.hours), "elapsed_h": elapsed, "target_h": target,
            "remaining_p50_h": p50, "remaining_p90_h": p90,
            "eta_p50": now + timedelta(hours=p50), "eta_p90": now + timedelta(hours=p90),
            "risk_miss": risk, "flag": flag, "stuck": stuck, "history_n": int(n),
        })
    df = pd.DataFrame(out, columns=cols)
    order = {"breached": 0, "at_risk": 1, "stuck": 2, "on_track": 3}
    if not df.empty:
        df = df.sort_values(by=["flag", "risk_miss"], key=lambda s: s.map(order) if s.name == "flag" else -s)
    return df.reset_index(drop=True)


# --------------------------------------------------------------------------- #
# 4. How good are the predictions? (walk-forward backtest)
# --------------------------------------------------------------------------- #
def backtest(intervals: pd.DataFrame, settings: Settings) -> dict:
    """For every completed request, at every point it entered a new stage, predict the
    remaining time using ONLY history that had finished by then, and compare with what
    actually happened. Baseline for comparison: "median past cycle time minus time
    already elapsed". Reports mean absolute error and bias (hours), how often the real
    finish came before the predicted 90th percentile, and precision / recall of the
    "at risk" flag."""
    empty = {"points": 0, "requests": 0, "mae_h": None, "baseline_mae_h": None,
             "bias_h": None, "p90_coverage": None, "risk_precision": None, "risk_recall": None, "actual_miss_rate": None}
    if intervals.empty:
        return empty
    closed = intervals[~intervals["is_open"]]
    pred = Predictor(closed, settings)
    done = closed[closed["outcome"] == "completed"]
    finish = done.groupby(["source", "request_id"])["end"].max()
    first = done.groupby(["source", "request_id"])["submitted_at"].first()
    cycles = pd.DataFrame({"finish": finish, "submitted": first})
    cycles["cycle_h"] = (cycles["finish"] - cycles["submitted"]).dt.total_seconds() / 3600
    cycles = cycles.sort_values("finish")
    finish_ns = cycles["finish"].dt.tz_convert(None).to_numpy(dtype="datetime64[ns]")
    cycle_hours = cycles["cycle_h"].to_numpy(float)

    errs, signed, base_errs, covered, flags, misses = [], [], [], [], [], []
    for (source, rid), g in done.groupby(["source", "request_id"]):
        g = g.sort_values("start")
        end_time = finish.loc[(source, rid)].to_pydatetime()
        submitted = g["submitted_at"].iloc[0].to_pydatetime()
        target = float(settings.target_hours.get(g["type"].iloc[0], 168))
        missed = _hours(end_time - submitted) > target
        for r in g.itertuples(index=False):
            t = r.start.to_pydatetime()
            k = int(np.searchsorted(finish_ns, _ns(t), side="left"))
            if k < settings.min_history:
                continue                      # not enough history at that moment
            sched = None
            if r.stage == "scheduled_wait" and not pd.isna(r.end):
                sched = _hours(r.end.to_pydatetime() - t)      # appointment time was known then
            samples, _ = pred.sample_remaining(r.type, r.stage, 0.0, _rng(source, rid, f"bt-{r.stage}"),
                                               settings.backtest_draws, cutoff=t, scheduled_until_h=sched)
            actual = _hours(end_time - t)
            elapsed = _hours(t - submitted)
            errs.append(abs(float(np.median(samples)) - actual))
            signed.append(float(np.median(samples)) - actual)
            base_errs.append(abs(max(float(np.median(cycle_hours[:k])) - elapsed, 0.0) - actual))
            covered.append(actual <= float(np.quantile(samples, .9)))
            if elapsed <= target:             # risk flags only matter while the outcome is open
                flags.append(float(np.mean(elapsed + samples > target)) >= settings.risk_threshold)
                misses.append(missed)
    if not errs:
        return empty
    flags, misses = np.array(flags, bool), np.array(misses, bool)
    # Of the moments we said "at risk", how often did the request really miss its target
    # (precision)? Of the moments a request that DID miss was still within its target time,
    # how often had we already said so (recall)? Plain accuracy would look good just by
    # never flagging, since most requests finish on time.
    precision = float(misses[flags].mean()) if flags.any() else None
    recall = float(flags[misses].mean()) if misses.any() else None
    return {
        "points": len(errs), "requests": int(done[["source", "request_id"]].drop_duplicates().shape[0]),
        "mae_h": float(np.mean(errs)), "bias_h": float(np.mean(signed)),
        "baseline_mae_h": float(np.mean(base_errs)),
        "p90_coverage": float(np.mean(covered)), "risk_precision": precision, "risk_recall": recall,
        "actual_miss_rate": float(misses.mean()) if misses.size else None,
    }

