"""
Turns the raw store into the handful of numbers the dashboard actually shows.

This is the analytical core of the project, and it lives here -- in Python, run
once per day -- rather than in the browser. Three reasons:

1. The interesting signals need history. Acute-to-chronic workload ratio reads
   28 days of training load; a resting-HR baseline is only meaningful against a
   month of mornings. Shipping a month of raw Garmin payloads to the browser to
   recompute them on every page load would be absurd.
2. Computed once, the numbers are stable and auditable. The dashboard renders
   what the pipeline decided; it doesn't re-derive it and possibly disagree.
3. It's testable. Every function below is pure -- plain data in, plain data out
   -- which is what tests/test_signals.py exercises.

Output contract: web/data.json. That file is the only thing the presentation
layer reads, which is what makes the frontend swappable and the demo dataset
possible.

A note on what this device actually reports
-------------------------------------------
Frank's Garmin returns no HRV data and no Training Readiness score (both come
back empty, not errored). So the readiness model here is built from what the
watch *does* report -- resting HR against its own baseline, sleep duration and
stage composition, body battery at wake, stress, and computed training load.
Every readiness score carries the list of inputs that were actually available,
so a score built from three signals is never mistaken for one built from five.

Usage:
  python3 signals.py            # write web/data.json
  python3 signals.py --print    # also dump a summary to stdout
"""

import argparse
import json
import math
import statistics
from datetime import date, datetime, timedelta
from pathlib import Path

import store

BASE = Path(__file__).resolve().parent
PLAN_JSON = BASE / "plan" / "plan.json"
CONFIG_JSON = BASE / "config.json"
OUT_JSON = BASE / "web" / "data.json"

METERS_PER_MILE = 1609.344
MARATHON_MILES = 26.2188

# Fallback TRIMP-per-minute when an activity has no average heart rate.
# Calibrated so a moderate session (~60% of heart-rate reserve) lands near the
# value the HR formula would produce; running is weighted highest because it
# carries the most mechanical cost per minute.
SPORT_LOAD_PER_MIN = {
    "running": 1.4,
    "trail_running": 1.4,
    "treadmill_running": 1.35,
    "cycling": 1.0,
    "indoor_cycling": 1.0,
    "virtual_ride": 1.0,
    "lap_swimming": 1.2,
    "open_water_swimming": 1.25,
    "indoor_cardio": 0.9,
    "strength_training": 0.7,
    "walking": 0.4,
    "other": 0.8,
}
DEFAULT_LOAD_PER_MIN = 0.8

RUN_TYPES = {"running", "trail_running", "treadmill_running", "track_running"}


# --------------------------------------------------------------------------
# small helpers
# --------------------------------------------------------------------------

def load_json(path: Path, default):
    try:
        return json.loads(path.read_text())
    except Exception:
        return default


def get_path(d, *keys, default=None):
    cur = d
    for k in keys:
        if not isinstance(cur, dict):
            return default
        cur = cur.get(k)
        if cur is None:
            return default
    return cur


def mean(values):
    values = [v for v in values if v is not None]
    return sum(values) / len(values) if values else None


def median(values):
    values = [v for v in values if v is not None]
    return statistics.median(values) if values else None


def round_or_none(value, digits=1):
    return None if value is None else round(value, digits)


def clamp(value, low, high):
    return max(low, min(high, value))


def fmt_pace(seconds_per_mile):
    """Seconds per mile -> 'M:SS'."""
    if seconds_per_mile is None or seconds_per_mile <= 0 or math.isinf(seconds_per_mile):
        return None
    total = int(round(seconds_per_mile))
    return f"{total // 60}:{total % 60:02d}"


def fmt_duration(seconds):
    """Seconds -> 'H:MM:SS'."""
    if seconds is None:
        return None
    total = int(round(seconds))
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}"


def date_range(start: date, end: date):
    cur = start
    while cur <= end:
        yield cur
        cur += timedelta(days=1)


# --------------------------------------------------------------------------
# daily wellness metrics
# --------------------------------------------------------------------------

def extract_day_metrics(day_str: str, payload: dict) -> dict:
    """Flatten one stored day into the handful of fields we care about."""
    payload = payload or {}
    stats = payload.get("stats") or {}
    sleep_dto = get_path(payload, "sleep", "dailySleepDTO", default={}) or {}
    hrv_summary = get_path(payload, "hrv", "hrvSummary", default={}) or {}
    stress = payload.get("stress") or {}
    readiness = payload.get("training_readiness") or []
    readiness = readiness[0] if isinstance(readiness, list) and readiness else {}

    sleep_seconds = sleep_dto.get("sleepTimeSeconds")
    deep = sleep_dto.get("deepSleepSeconds")
    rem = sleep_dto.get("remSleepSeconds")

    return {
        "date": day_str,
        "source": payload.get("_source", "api"),
        "rhr": stats.get("restingHeartRate"),
        "rhr_7day_avg": stats.get("lastSevenDaysAvgRestingHeartRate"),
        "sleep_seconds": sleep_seconds,
        "sleep_hours": round_or_none(sleep_seconds / 3600, 2) if sleep_seconds else None,
        "sleep_score": get_path(sleep_dto, "sleepScores", "overall", "value"),
        "deep_seconds": deep,
        "rem_seconds": rem,
        "light_seconds": sleep_dto.get("lightSleepSeconds"),
        "awake_seconds": sleep_dto.get("awakeSleepSeconds"),
        "deep_pct": round_or_none(100 * deep / sleep_seconds, 1) if deep and sleep_seconds else None,
        "rem_pct": round_or_none(100 * rem / sleep_seconds, 1) if rem and sleep_seconds else None,
        "bb_wake": stats.get("bodyBatteryAtWakeTime"),
        "bb_high": stats.get("bodyBatteryHighestValue"),
        "bb_low": stats.get("bodyBatteryLowestValue"),
        "stress_avg": _clean_stress(stress.get("avgStressLevel"), stats.get("averageStressLevel")),
        "respiration_avg": stats.get("avgWakingRespirationValue"),
        "steps": stats.get("totalSteps"),
        # Present in the schema but empty on this device -- kept so the
        # dashboard can say "unavailable" rather than pretend the field is 0.
        "hrv_last_night": hrv_summary.get("lastNightAvg"),
        "hrv_status": hrv_summary.get("status"),
        "garmin_readiness": readiness.get("score"),
    }


def _clean_stress(*candidates):
    for value in candidates:
        if value not in (None, -1):
            return value
    return None


# --------------------------------------------------------------------------
# training load
# --------------------------------------------------------------------------

def estimate_hr_max(activities: list, configured=None) -> dict:
    """Best available HRmax, and an honest label for where it came from."""
    if configured:
        return {"value": configured, "method": "configured"}
    observed = [a.get("maxHR") for a in activities if a.get("maxHR")]
    if observed:
        # The highest HR ever recorded in a session is a defensible floor for
        # HRmax -- it's a number he demonstrably hit.
        return {"value": max(observed), "method": "highest observed in a session"}
    return {"value": 190, "method": "generic default (no HR data available)"}


def activity_load(activity: dict, hr_max: float, resting_hr: float) -> dict:
    """Training load for one session, as Banister TRIMP where HR allows.

        TRIMP = minutes x HRr x 0.64 x e^(1.92 x HRr)
        HRr   = (avgHR - restingHR) / (HRmax - restingHR)

    The exponential is the point: it weights hard minutes far more heavily than
    easy ones, so an hour of intervals and an hour of jogging don't score alike.
    Sessions without average HR fall back to a per-sport rate and are flagged
    ``estimated`` so the dashboard can mark them.
    """
    duration_s = activity.get("duration") or 0
    minutes = duration_s / 60
    if minutes <= 0:
        return {"load": 0.0, "estimated": True, "minutes": 0.0}

    avg_hr = activity.get("averageHR")
    type_key = (activity.get("activityType") or {}).get("typeKey") or "other"

    if avg_hr and hr_max and resting_hr and hr_max > resting_hr:
        hr_reserve = clamp((avg_hr - resting_hr) / (hr_max - resting_hr), 0.0, 1.0)
        load = minutes * hr_reserve * 0.64 * math.exp(1.92 * hr_reserve)
        return {"load": round(load, 1), "estimated": False, "minutes": round(minutes, 1),
                "hr_reserve": round(hr_reserve, 3)}

    rate = SPORT_LOAD_PER_MIN.get(type_key, DEFAULT_LOAD_PER_MIN)
    return {"load": round(minutes * rate, 1), "estimated": True, "minutes": round(minutes, 1)}


def daily_loads(activities: list, hr_max: float, resting_hr: float,
                start: date, end: date) -> dict:
    """{date_iso: total load} for every day in the window, zeros included.

    The zeros matter: rest days are real data for monotony and for a rolling
    average, so the window is dense rather than sparse.
    """
    totals = {d.isoformat(): 0.0 for d in date_range(start, end)}
    for activity in activities:
        day = store.activity_date(activity)
        if day is None or not (start <= day <= end):
            continue
        totals[day.isoformat()] += activity_load(activity, hr_max, resting_hr)["load"]
    return {k: round(v, 1) for k, v in totals.items()}


def acwr(loads_by_date: dict, as_of: date) -> dict:
    """Acute:chronic workload ratio, plus monotony and strain.

    Acute   = total load over the last 7 days (including as_of).
    Chronic = average *weekly* load over the last 28 days, i.e. 28-day total / 4.
    Ratio   = acute / chronic.

    The widely used interpretation (Gabbett) puts the 'sweet spot' at roughly
    0.8-1.3: enough stimulus to adapt, not enough of a spike to raise injury
    risk. Above ~1.5 is a sharp ramp. Below 0.8 means detraining -- which is
    exactly where a post-race re-entry week should sit, so the label matters
    more than the number.

    Monotony (Foster) = mean daily load / standard deviation of daily load over
    the week. High monotony means every day looks the same, which is a
    surprisingly good predictor of staleness. Strain = acute load x monotony.
    """
    acute_days = [(as_of - timedelta(days=i)).isoformat() for i in range(7)]
    chronic_days = [(as_of - timedelta(days=i)).isoformat() for i in range(28)]

    acute_values = [loads_by_date.get(d, 0.0) for d in acute_days]
    chronic_values = [loads_by_date.get(d) for d in chronic_days]
    chronic_present = [v for v in chronic_values if v is not None]

    acute = round(sum(acute_values), 1)
    # Only claim a chronic baseline if we really have most of the 28 days.
    if len(chronic_present) < 21:
        return {
            "acute_7day": acute,
            "chronic_28day_weekly": None,
            "ratio": None,
            "zone": "insufficient_history",
            "label": f"Need 21+ days of history for a workload ratio (have {len(chronic_present)}).",
            "monotony": None,
            "strain": None,
            "days_of_history": len(chronic_present),
        }

    chronic_weekly = sum(chronic_present) / len(chronic_present) * 7
    ratio = (acute / chronic_weekly) if chronic_weekly > 0 else None

    if ratio is None:
        zone, label = "insufficient_history", "No training load recorded in the last 28 days."
    elif ratio < 0.8:
        zone, label = "detraining", "Load is below your 4-week baseline — fine for a taper or re-entry week, detraining if it persists."
    elif ratio <= 1.3:
        zone, label = "optimal", "Load is in the productive range against your 4-week baseline."
    elif ratio <= 1.5:
        zone, label = "caution", "Load is ramping faster than your baseline supports — hold volume steady this week."
    else:
        zone, label = "high_risk", "Sharp load spike versus your 4-week baseline — back off before something breaks."

    stdev = statistics.pstdev(acute_values) if len(acute_values) > 1 else 0.0
    monotony = (mean(acute_values) / stdev) if stdev > 0 else None
    strain = (acute * monotony) if monotony else None

    return {
        "acute_7day": acute,
        "chronic_28day_weekly": round(chronic_weekly, 1),
        "ratio": round_or_none(ratio, 2),
        "zone": zone,
        "label": label,
        "monotony": round_or_none(monotony, 2),
        "strain": round_or_none(strain, 0),
        "days_of_history": len(chronic_present),
    }


# --------------------------------------------------------------------------
# baselines and readiness
# --------------------------------------------------------------------------

def rhr_baseline(history: list, as_of: str) -> dict:
    """Resting HR today against a trailing 28-day median of prior days.

    Median rather than mean because one bad night (or one missing-sensor
    artifact) shouldn't drag the baseline it's being judged against. A z-score
    is reported too, so '+3 bpm' can be read as either routine noise or a real
    signal depending on how stable his mornings normally are.
    """
    prior = [h for h in history if h["date"] < as_of][-28:]
    values = [h["rhr"] for h in prior if h["rhr"] is not None]
    today = next((h["rhr"] for h in history if h["date"] == as_of), None)

    if not values:
        return {"today": today, "baseline": None, "delta": None, "z": None, "n": 0}

    baseline = statistics.median(values)
    stdev = statistics.pstdev(values) if len(values) > 1 else 0.0
    delta = (today - baseline) if today is not None else None
    z = (delta / stdev) if (delta is not None and stdev > 0) else None

    return {
        "today": today,
        "baseline": round(baseline, 1),
        "delta": round_or_none(delta, 1),
        "z": round_or_none(z, 2),
        "stdev": round(stdev, 2),
        "n": len(values),
    }


def sleep_summary(history: list, as_of: str, target_hours: float) -> dict:
    """Last night plus a 7-night rolling picture and a running sleep debt."""
    last_night = next((h for h in history if h["date"] == as_of), None) or {}
    window = [h for h in history if h["date"] <= as_of][-7:]
    hours = [h["sleep_hours"] for h in window if h["sleep_hours"] is not None]

    avg = mean(hours)
    debt = (target_hours * len(hours) - sum(hours)) if hours else None

    return {
        "last_night_hours": last_night.get("sleep_hours"),
        "last_night_score": last_night.get("sleep_score"),
        "deep_pct": last_night.get("deep_pct"),
        "rem_pct": last_night.get("rem_pct"),
        "avg_7day_hours": round_or_none(avg, 2),
        "debt_7day_hours": round_or_none(debt, 1),
        "nights_recorded": len(hours),
        "target_hours": target_hours,
    }


def _score_rhr(delta):
    """Lower-than-baseline resting HR is good; +5bpm or worse scores zero."""
    if delta is None:
        return None
    return clamp(1 - (delta + 2) / 7, 0.0, 1.0)


def _score_sleep(hours, target):
    if hours is None:
        return None
    # Full marks at target, zero at 3 hours below it.
    return clamp(1 - (target - hours) / 3, 0.0, 1.0)


def _score_body_battery(wake_value):
    if wake_value is None:
        return None
    # Waking above 70 is a good night's recharge; below 20 is not.
    return clamp((wake_value - 20) / 50, 0.0, 1.0)


def _score_stress(stress_avg):
    if stress_avg is None:
        return None
    return clamp(1 - (stress_avg - 25) / 35, 0.0, 1.0)


def _score_load(ratio):
    if ratio is None:
        return None
    if 0.8 <= ratio <= 1.3:
        return 1.0
    if ratio < 0.8:
        # Under-loaded isn't a recovery problem, so this stays generous.
        return clamp(0.7 + (ratio / 0.8) * 0.3, 0.0, 1.0)
    return clamp(1 - (ratio - 1.3) / 0.7, 0.0, 1.0)


def readiness(rhr: dict, sleep: dict, day: dict, load: dict, weights: dict) -> dict:
    """A 0-100 composite of the recovery signals this device actually reports.

    Each component scores 0-1, then contributes its configured weight. Missing
    components are dropped and the remaining weights are renormalised, so the
    score is always on a 0-100 scale -- and ``inputs_used`` records which
    signals were really behind it, because a score built from two inputs
    deserves less trust than one built from five.

    This is a heuristic, not a validated physiological model, and the dashboard
    says so.
    """
    components = {
        "resting_hr": _score_rhr(rhr.get("delta")),
        "sleep": _score_sleep(sleep.get("last_night_hours") or sleep.get("avg_7day_hours"),
                              sleep.get("target_hours", 7.5)),
        # Wake value is the cleanest read, but it's absent on a partially
        # synced day; the day's peak is a fair stand-in since the peak
        # almost always occurs at wake.
        "body_battery": _score_body_battery(day.get("bb_wake") or day.get("bb_high")),
        "stress": _score_stress(day.get("stress_avg")),
        "training_load": _score_load(load.get("ratio")),
    }

    available = {k: v for k, v in components.items() if v is not None}
    if not available:
        return {"score": None, "band": "unknown", "label": "Not enough recovery data to score today.",
                "components": components, "inputs_used": [], "weights_applied": {}}

    total_weight = sum(weights.get(k, 0) for k in available)
    if total_weight <= 0:
        return {"score": None, "band": "unknown", "label": "No readiness weights configured.",
                "components": components, "inputs_used": sorted(available), "weights_applied": {}}

    score = sum(v * weights.get(k, 0) for k, v in available.items()) / total_weight * 100

    if score >= 75:
        band, label = "good", "Recovery signals are strong — take the session as planned."
    elif score >= 55:
        band, label = "moderate", "Recovery is adequate — start the session and let feel decide the last rep."
    elif score >= 40:
        band, label = "low", "Recovery is lagging — keep it aerobic and move quality to tomorrow."
    else:
        band, label = "poor", "Recovery signals are poor — rest or an easy shakeout only."

    return {
        "score": round(score),
        "band": band,
        "label": label,
        "components": {k: round_or_none(v, 3) for k, v in components.items()},
        "inputs_used": sorted(available),
        "inputs_missing": sorted(k for k, v in components.items() if v is None),
        "weights_applied": {k: weights.get(k, 0) for k in sorted(available)},
    }


# --------------------------------------------------------------------------
# running fitness and race projection
# --------------------------------------------------------------------------

def is_run(activity: dict) -> bool:
    type_key = (activity.get("activityType") or {}).get("typeKey") or ""
    return type_key in RUN_TYPES or "running" in type_key


def run_summary(activity: dict) -> dict:
    distance_m = activity.get("distance") or 0
    duration_s = activity.get("duration") or 0
    miles = distance_m / METERS_PER_MILE
    pace = (duration_s / miles) if miles > 0.3 else None
    avg_hr = activity.get("averageHR")

    return {
        "date": (store.activity_date(activity) or date.min).isoformat(),
        "name": activity.get("activityName"),
        "miles": round(miles, 2),
        "duration_seconds": round(duration_s),
        "pace_seconds_per_mile": round_or_none(pace, 0),
        "pace": fmt_pace(pace),
        "avg_hr": avg_hr,
        "max_hr": activity.get("maxHR"),
        # Speed per heartbeat: rises as aerobic fitness improves, because the
        # same heart rate carries you further. Scaled x1000 to be readable.
        "efficiency_index": round(1000 * (miles / (duration_s / 3600)) / avg_hr, 1)
        if (avg_hr and duration_s > 0 and miles > 0.3) else None,
        "source": activity.get("_source", "api"),
    }


def weekly_mileage(activities: list, week_starts: list) -> list:
    """Actual run miles per plan week."""
    out = []
    for start_iso in week_starts:
        start = datetime.fromisoformat(start_iso).date()
        end = start + timedelta(days=6)
        miles = 0.0
        load_minutes = 0.0
        for activity in activities:
            day = store.activity_date(activity)
            if day is None or not (start <= day <= end) or not is_run(activity):
                continue
            miles += (activity.get("distance") or 0) / METERS_PER_MILE
            load_minutes += (activity.get("duration") or 0) / 60
        out.append({"week_start": start_iso, "miles": round(miles, 1),
                    "run_minutes": round(load_minutes)})
    return out


def riegel_prediction(runs: list, target_miles=MARATHON_MILES, exponent=1.06) -> dict:
    """Project a marathon time from recent runs using Riegel's formula.

        T2 = T1 x (D2 / D1) ^ 1.06

    The 1.06 exponent is Riegel's empirical fatigue factor. The formula assumes
    the input was a *maximal* effort, which a training run never quite is, so
    this reports three reads rather than one number:

      predicted    -- from the fastest-projecting run, i.e. the effort closest
                      to maximal. This is the headline estimate.
      from_longest -- from the longest run in the window. Riegel is most
                      reliable extrapolating from longer distances, so this is
                      the most methodologically sound read even when it isn't
                      the fastest.
      conservative -- from the slowest-projecting run, included only to show
                      the spread across easy days.

    Only runs of 5+ miles are considered; below that Riegel gets optimistic fast.
    """
    candidates = [r for r in runs if r["miles"] >= 5 and r["duration_seconds"] > 0]
    if not candidates:
        return {"predicted_seconds": None, "basis": None,
                "note": "No run of 5+ miles in the window to project from."}

    def project(run):
        return run["duration_seconds"] * (target_miles / run["miles"]) ** exponent

    def read(run):
        seconds = project(run)
        return {
            "seconds": round(seconds),
            "time": fmt_duration(seconds),
            "pace": fmt_pace(seconds / target_miles),
            "basis": {"date": run["date"], "name": run["name"],
                      "miles": run["miles"], "pace": run["pace"], "avg_hr": run["avg_hr"]},
        }

    best = read(min(candidates, key=project))
    worst = read(max(candidates, key=project))
    longest = read(max(candidates, key=lambda r: r["miles"]))

    return {
        "predicted_seconds": best["seconds"],
        "predicted": best["time"],
        "predicted_pace": best["pace"],
        "basis": best["basis"],
        "from_longest": longest,
        "conservative": worst,
        "spread": [best["time"], worst["time"]],
        "candidates_considered": len(candidates),
        "note": "Riegel extrapolation (exponent 1.06) from training runs of 5+ miles in the "
                "last 6 weeks. Riegel assumes a maximal effort, so a projection off training "
                "runs reads pessimistic — treat it as a floor, not a forecast.",
    }


def fitness_summary(activities: list, as_of: date) -> dict:
    window_start = as_of - timedelta(days=41)
    runs = [run_summary(a) for a in activities
            if is_run(a) and (store.activity_date(a) or date.min) >= window_start
            and (store.activity_date(a) or date.min) <= as_of]
    runs.sort(key=lambda r: r["date"])

    last_28 = [r for r in runs if r["date"] >= (as_of - timedelta(days=27)).isoformat()]
    efficiencies = [r["efficiency_index"] for r in runs if r["efficiency_index"] is not None]

    # Split the efficiency series in half to show direction of travel rather
    # than a single point-in-time number.
    trend = None
    if len(efficiencies) >= 4:
        half = len(efficiencies) // 2
        first, second = mean(efficiencies[:half]), mean(efficiencies[half:])
        if first:
            trend = round(100 * (second - first) / first, 1)

    return {
        "runs_28day": len(last_28),
        "miles_28day": round(sum(r["miles"] for r in last_28), 1),
        "longest_run_28day": max((r["miles"] for r in last_28), default=0),
        "avg_pace_28day": fmt_pace(mean([r["pace_seconds_per_mile"] for r in last_28])),
        "efficiency_index_latest": efficiencies[-1] if efficiencies else None,
        "efficiency_trend_pct": trend,
        "recent_runs": runs[-10:],
        "projection": riegel_prediction(runs),
    }


# --------------------------------------------------------------------------
# plan adherence
# --------------------------------------------------------------------------

def adherence(plan: dict, activities: list, as_of: date) -> dict:
    """How the block is actually going versus how it was drawn up.

    Two measures, because they answer different questions: session completion
    (did the workouts happen) and weekly mileage against target (was the volume
    actually there). A week can be 100% on sessions and 70% on volume.
    """
    plan_days = plan.get("days", {})
    keywords = [k.lower() for k in plan.get("key_session_keywords", [])]
    weeks_meta = plan.get("weeks", [])

    activity_dates = set()
    for activity in activities:
        day = store.activity_date(activity)
        if day:
            activity_dates.add(day.isoformat())

    actual_by_week = {w["week_start"]: w for w in
                      weekly_mileage(activities, [w["start"] for w in weeks_meta])}

    week_rows = []
    for week in weeks_meta:
        start = datetime.fromisoformat(week["start"]).date()
        end = datetime.fromisoformat(week["end"]).date()
        # Today is still in progress, so a session planned for today is not yet
        # "missed" -- adherence only judges days that have actually finished.
        last_judged = min(end, as_of - timedelta(days=1))
        elapsed_days = [d for d in date_range(start, last_judged)] if last_judged >= start else []

        planned_key = 0
        completed_key = 0
        for day in elapsed_days:
            entry = plan_days.get(day.isoformat())
            if not entry:
                continue
            session = (entry.get("session") or "").lower()
            if any(k in session for k in keywords):
                planned_key += 1
                if day.isoformat() in activity_dates:
                    completed_key += 1

        actual_week = actual_by_week.get(week["start"], {})
        week_rows.append({
            "week": week["week"],
            "phase": week["phase"],
            "headline": week["headline"],
            "start": week["start"],
            "end": week["end"],
            "status": "done" if end < as_of else ("current" if start <= as_of <= end else "upcoming"),
            "miles_target": week["miles_target"],
            "miles_actual": actual_week.get("miles", 0.0),
            "key_sessions_due": planned_key,
            "key_sessions_done": completed_key,
        })

    elapsed = [w for w in week_rows if w["status"] in ("done", "current")]
    due = sum(w["key_sessions_due"] for w in elapsed)
    done = sum(w["key_sessions_done"] for w in elapsed)

    return {
        "weeks": week_rows,
        "key_sessions_due_to_date": due,
        "key_sessions_done_to_date": done,
        "completion_pct": round(100 * done / due) if due else None,
    }


# --------------------------------------------------------------------------
# fuel
# --------------------------------------------------------------------------

def fuel_targets(plan: dict, today_entry: dict, weight_kg) -> dict:
    """Today's carb target, scaled by what today's session actually is.

    Deliberately scoped: Garmin carries no food logging, so inventing calorie
    tracking here would be fiction. What *can* be derived honestly is the
    carbohydrate target implied by the day's training, which is the number that
    actually matters in a marathon block.
    """
    fuel = plan.get("fuel", {})
    baseline = fuel.get("daily_baseline", {})
    session = (today_entry or {}).get("session", "").lower()

    if "race day" in session:
        band, per_kg = "race day", 8.0
    elif "long run" in session:
        band, per_kg = "long run day", 8.0
    elif any(k in session for k in ("quality", "tempo", "intervals", "sharpener", "medium-long")):
        band, per_kg = "quality day", 6.5
    elif "rest" in session and "easy" not in session:
        band, per_kg = "rest day", 4.0
    else:
        band, per_kg = "easy day", 4.5

    grams = round(per_kg * weight_kg) if weight_kg else None

    return {
        "band": band,
        "carbs_per_kg": per_kg,
        "carbs_grams": grams,
        "protein_per_kg": baseline.get("protein", "1.6–2.0g/kg/day"),
        "weight_kg": weight_kg,
        "weight_configured": weight_kg is not None,
        "race_day": fuel.get("race_day", {}),
        "carb_load": fuel.get("carb_load", {}),
        "note": "Carbohydrate targets derived from the day's planned session. "
                "Calorie and macro *intake* is not tracked — Garmin carries no food log, "
                "so the dashboard reports targets rather than inventing consumption data.",
    }


# --------------------------------------------------------------------------
# assembly
# --------------------------------------------------------------------------

def build(as_of: date = None, days=None, activities=None, plan=None, config=None) -> dict:
    as_of = as_of or date.today()
    days = days if days is not None else store.all_days()
    activities = activities if activities is not None else store.all_activities()
    plan = plan if plan is not None else load_json(PLAN_JSON, {"days": {}, "race": {}})
    config = config if config is not None else load_json(CONFIG_JSON, {})

    weights = config.get("readiness_weights") or {
        "resting_hr": 30, "sleep": 30, "body_battery": 15, "stress": 10, "training_load": 15}
    sleep_target = config.get("sleep_target_hours") or 7.5
    weight_kg = config.get("weight_kg")

    history = [extract_day_metrics(d, days[d]) for d in sorted(days.keys())]
    as_of_iso = as_of.isoformat()

    # The most recent day we actually hold data for -- today's numbers may not
    # have synced yet, and showing yesterday honestly beats showing blanks.
    latest = next((h for h in reversed(history) if h["date"] <= as_of_iso), None) or \
        extract_day_metrics(as_of_iso, {})

    hr_max_info = estimate_hr_max(activities, config.get("hr_max"))
    rhr_info = rhr_baseline(history, latest["date"])
    resting_for_trimp = rhr_info.get("baseline") or latest.get("rhr") or 55

    loads = daily_loads(activities, hr_max_info["value"], resting_for_trimp,
                        as_of - timedelta(days=55), as_of)
    load_info = acwr(loads, as_of)
    sleep_info = sleep_summary(history, latest["date"], sleep_target)
    readiness_info = readiness(rhr_info, sleep_info, latest, load_info, weights)
    fitness = fitness_summary(activities, as_of)
    adherence_info = adherence(plan, activities, as_of)

    race = plan.get("race", {})
    days_to_race = None
    if race.get("date"):
        try:
            days_to_race = (datetime.fromisoformat(race["date"]).date() - as_of).days
        except Exception:
            pass

    goal_seconds = race.get("goal_seconds")
    predicted = fitness["projection"].get("predicted_seconds")
    gap = (predicted - goal_seconds) if (predicted and goal_seconds) else None

    today_entry = plan.get("days", {}).get(as_of_iso)
    current_week = next((w for w in adherence_info["weeks"] if w["status"] == "current"), None)

    return {
        "meta": {
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "as_of": as_of_iso,
            "data_through": latest["date"],
            "store_coverage": {
                "day_count": len(history),
                "first_day": history[0]["date"] if history else None,
                "last_day": history[-1]["date"] if history else None,
                "activity_count": len(activities),
            },
            "hr_max": hr_max_info,
            "unavailable_signals": _unavailable(history),
            "schema_version": 2,
        },
        "race": {**race, "days_to_race": days_to_race},
        "paces": plan.get("paces", {}),
        "block": plan.get("block", {}),
        "today": {
            "date": as_of_iso,
            "plan": today_entry,
            "week": current_week,
        },
        "readiness": readiness_info,
        "recovery": {
            "latest": latest,
            "resting_hr": rhr_info,
            "sleep": sleep_info,
            "history": history[-28:],
        },
        "load": {
            **load_info,
            "daily": {k: v for k, v in sorted(loads.items()) if k >= (as_of - timedelta(days=41)).isoformat()},
            "method": "Banister TRIMP from average HR where available, per-sport rate otherwise.",
        },
        "fitness": fitness,
        "projection": {
            **fitness["projection"],
            "goal_seconds": goal_seconds,
            "goal": race.get("goal"),
            "gap_seconds": gap,
            "gap": (("+" if gap > 0 else "−") + fmt_duration(abs(gap))) if gap else None,
            "on_track": (gap is not None and gap <= 0),
        },
        "adherence": adherence_info,
        # Dates with any recorded activity, so the plan view can mark a session
        # done without the browser re-deriving it from the activity list.
        "completed_days": sorted(
            {d.isoformat() for d in (store.activity_date(a) for a in activities) if d}
        ),
        "fuel": fuel_targets(plan, today_entry, weight_kg),
        "plan_days": plan.get("days", {}),
    }


def _unavailable(history: list) -> list:
    """Signals the schema supports but this device never actually fills in."""
    recent = history[-14:]
    out = []
    if recent and not any(h["hrv_last_night"] for h in recent):
        out.append({"signal": "HRV", "reason": "Garmin returns no HRV data for this device — readiness is composed without it."})
    if recent and not any(h["garmin_readiness"] for h in recent):
        out.append({"signal": "Garmin Training Readiness", "reason": "Not reported for this device — this dashboard computes its own readiness score instead."})
    if recent and not any(h["sleep_score"] for h in recent):
        out.append({"signal": "Garmin sleep score", "reason": "Not reported — sleep is scored here from duration and stage composition."})
    return out


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--print", action="store_true", dest="do_print", help="Print a summary")
    parser.add_argument("--as-of", help="Compute as of this date (YYYY-MM-DD), for testing")
    args = parser.parse_args()

    as_of = datetime.fromisoformat(args.as_of).date() if args.as_of else date.today()
    signals = build(as_of=as_of)

    OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    OUT_JSON.write_text(json.dumps(signals, indent=2, default=str))
    print(f"Signals written to {OUT_JSON}")

    if args.do_print:
        r, l, p = signals["readiness"], signals["load"], signals["projection"]
        print(f"\n  as of {signals['meta']['as_of']} (data through {signals['meta']['data_through']})")
        print(f"  readiness      {r['score']} ({r['band']}) from {', '.join(r['inputs_used'])}")
        print(f"  resting HR     {signals['recovery']['resting_hr']['today']} vs baseline "
              f"{signals['recovery']['resting_hr']['baseline']} (delta {signals['recovery']['resting_hr']['delta']})")
        print(f"  sleep 7d avg   {signals['recovery']['sleep']['avg_7day_hours']}h")
        print(f"  ACWR           {l['ratio']} [{l['zone']}] acute {l['acute_7day']} / chronic {l['chronic_28day_weekly']}")
        print(f"  28d run miles  {signals['fitness']['miles_28day']} (longest {signals['fitness']['longest_run_28day']})")
        print(f"  projection     {p.get('predicted')} vs goal {p.get('goal')} (gap {p.get('gap')})")
        print(f"  adherence      {signals['adherence']['key_sessions_done_to_date']}/"
              f"{signals['adherence']['key_sessions_due_to_date']} key sessions")
        print(f"  race in        {signals['race'].get('days_to_race')} days")
        for item in signals["meta"]["unavailable_signals"]:
            print(f"  unavailable: {item['signal']}")


if __name__ == "__main__":
    main()
