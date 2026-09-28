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
Probed directly against the account: ``get_hrv_data`` and ``get_hrv_data_range``
both return ``{}``, and ``get_training_readiness``, ``get_morning_training_readiness``
and ``get_endurance_score`` are all empty too. Garmin's sleep response carries no
sleep score either. These come back *empty rather than errored*, which is how they
quietly become a chart of zeros if nobody checks.

So there is no HRV here, and no amount of pipeline work will produce it. What the
sleep response *does* carry, and the old pipeline was throwing away, is a
full overnight time series: heart rate every ~2 minutes, plus overnight stress and
body battery. That gives a defensible stand-in for the autonomic signal HRV would
have provided -- the **nocturnal heart-rate drop**, the difference between heart
rate at sleep onset and the night's minimum. A big drop is parasympathetic rebound;
a shallow one is a body still working. It is not HRV and is never labelled as HRV,
but it moves for the same reasons.

Readiness is therefore composed from: resting HR against its own baseline, the
nocturnal drop against its own baseline, sleep duration and stage composition,
body battery at wake, stress, and computed training load. Every score carries the
list of inputs actually available, so a score built from three signals is never
mistaken for one built from five.

Usage:
  python3 signals.py            # write web/data.json
  python3 signals.py --print    # also dump a summary to stdout
"""

import argparse
import json
import math
import statistics
from collections import defaultdict
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

def extract_day_metrics(day_str: str, payload: dict, manual: dict = None) -> dict:
    """Flatten one stored day into the handful of fields we care about.

    ``manual`` fills gaps the watch left. Measured data always wins: Garmin
    sometimes backfills a night's sleep a day or two late, and when it does, the
    measurement should replace the estimate rather than the other way round. Any
    field that ends up coming from a manual entry is labelled, so the page can
    show it as entered rather than recorded.
    """
    payload = payload or {}
    manual = manual or {}
    stats = payload.get("stats") or {}
    sleep_dto = get_path(payload, "sleep", "dailySleepDTO", default={}) or {}
    hrv_summary = get_path(payload, "hrv", "hrvSummary", default={}) or {}
    stress = payload.get("stress") or {}
    readiness = payload.get("training_readiness") or []
    readiness = readiness[0] if isinstance(readiness, list) and readiness else {}

    sleep_seconds = sleep_dto.get("sleepTimeSeconds")
    deep = sleep_dto.get("deepSleepSeconds")
    rem = sleep_dto.get("remSleepSeconds")
    night = overnight_hr(payload)

    sleep_source = "garmin" if sleep_seconds else None
    if not sleep_seconds and manual.get("sleep_hours"):
        sleep_seconds = int(float(manual["sleep_hours"]) * 3600)
        sleep_source = manual.get("source", "manual")
        # Stage breakdown is not recoverable by hand, so it stays absent rather
        # than being invented from a typical ratio.
        deep = rem = None

    return {
        "date": day_str,
        "source": payload.get("_source", "api"),
        "sleep_source": sleep_source,
        "manual_note": manual.get("note"),
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
        # Derived from the overnight series -- the stand-in for HRV.
        "overnight_hr_min": night["min"],
        "overnight_hr_mean": night["mean"],
        "nocturnal_drop": night["drop"],
        "nocturnal_drop_timing": night["drop_timing"],
        "overnight_samples": night["samples"],
    }


def overnight_hr(payload: dict) -> dict:
    """Derive the nocturnal heart-rate picture from the overnight series.

    Garmin's sleep response includes ``sleepHeartRate`` -- roughly one sample
    every two minutes through the night. Three things come out of it:

      minimum     the night's lowest heart rate. A cleaner recovery signal than
                  the daily resting-HR figure, which is a whole-day statistic.
      drop        heart rate at sleep onset minus that minimum. This is the
                  stand-in for HRV: a deep drop means the parasympathetic system
                  took over properly, a shallow one means it didn't.
      drop_timing where in the night the minimum fell, 0 to 1. Well-recovered
                  nights bottom out early; a body still digesting a hard session
                  or a late meal bottoms out late, if at all.

    Returns empty values rather than guesses when the series is absent -- the
    legacy markdown-recovered days have no series at all.
    """
    series = (payload.get("sleep") or {}).get("sleepHeartRate") or []
    values = [p.get("value") for p in series if isinstance(p, dict) and p.get("value")]
    if len(values) < 10:
        return {"min": None, "mean": None, "spread": None, "drop": None,
                "drop_timing": None, "samples": len(values)}

    minimum = min(values)
    # Average the first ~30 minutes as the sleep-onset level; a single first
    # sample is too noisy to subtract from.
    onset_window = max(3, len(values) // 20)
    onset = sum(values[:onset_window]) / onset_window
    index_of_min = values.index(minimum)

    return {
        "min": minimum,
        "mean": round(sum(values) / len(values), 1),
        "spread": max(values) - minimum,
        "drop": round(onset - minimum, 1),
        "drop_timing": round(index_of_min / (len(values) - 1), 2),
        "samples": len(values),
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


def sport_load_rates(activities: list, hr_max: float, resting_hr: float) -> dict:
    """Load-per-minute for each sport, measured from this athlete's own sessions.

    The fixed constants in SPORT_LOAD_PER_MIN are a reasonable prior, but they're
    a guess about a generic athlete. Where there are enough HR-carrying sessions
    in a sport to measure it, the athlete's own median load per minute is a
    better estimate -- his easy rides really do sit where they sit. Sports with
    fewer than three measured sessions keep the prior.
    """
    by_sport = defaultdict(list)
    for activity in activities:
        if not activity.get("averageHR"):
            continue
        minutes = (activity.get("duration") or 0) / 60
        if minutes < 5:
            continue
        type_key = (activity.get("activityType") or {}).get("typeKey") or "other"
        result = activity_load(activity, hr_max, resting_hr, rates=None)
        if not result["estimated"] and result["minutes"] > 0:
            by_sport[type_key].append(result["load"] / result["minutes"])

    rates, measured = dict(SPORT_LOAD_PER_MIN), {}
    for type_key, samples in by_sport.items():
        if len(samples) >= 3:
            rates[type_key] = round(statistics.median(samples), 3)
            measured[type_key] = {"rate": rates[type_key], "sessions": len(samples)}
    return {"rates": rates, "measured": measured}


def activity_load(activity: dict, hr_max: float, resting_hr: float, rates: dict = None) -> dict:
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

    table = rates if rates is not None else SPORT_LOAD_PER_MIN
    rate = table.get(type_key, DEFAULT_LOAD_PER_MIN)
    return {"load": round(minutes * rate, 1), "estimated": True, "minutes": round(minutes, 1),
            "rate_source": "measured" if (rates and type_key in rates
                                          and rates[type_key] != SPORT_LOAD_PER_MIN.get(type_key))
                           else "prior"}


def daily_loads(activities: list, hr_max: float, resting_hr: float,
                start: date, end: date, rates: dict = None) -> dict:
    """{date_iso: total load} for every day in the window, zeros included.

    The zeros matter: rest days are real data for monotony and for a rolling
    average, so the window is dense rather than sparse.
    """
    totals = {d.isoformat(): 0.0 for d in date_range(start, end)}
    for activity in activities:
        day = store.activity_date(activity)
        if day is None or not (start <= day <= end):
            continue
        totals[day.isoformat()] += activity_load(activity, hr_max, resting_hr, rates)["load"]
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


def fitness_fatigue_form(loads_by_date: dict, as_of: date,
                         ctl_days=42, atl_days=7, seed_weeks=6) -> dict:
    """The impulse-response model: fitness, fatigue and form.

        CTL (fitness) = exponentially weighted mean of daily load, 42-day constant
        ATL (fatigue) = the same with a 7-day constant
        TSB (form)    = CTL - ATL

    This answers a different question from the workload ratio. ACWR asks "am I
    ramping faster than I can absorb" -- an injury-risk question. CTL/ATL/TSB
    asks "am I fit, and am I fresh enough to express it" -- a peaking question.
    A marathon block needs both: the ratio keeps you healthy through the build,
    form tells you whether the taper is working.

    Form reads negative through a build (fatigue outpaces fitness), climbs
    through a taper, and wants to be somewhere around +5 to +15 on race day.

    Seeded from the mean of the first `seed_weeks` of available load rather than
    from zero, because this history starts when the watch does. Starting at zero
    would draw a fake 6-week "fitness ramp" that is really just the model
    catching up to where he already was.
    """
    dates = sorted(loads_by_date)
    if not dates:
        return {"fitness": None, "fatigue": None, "form": None, "series": {},
                "label": "No training load recorded."}

    seed_window = dates[: seed_weeks * 7]
    seed = mean([loads_by_date[d] for d in seed_window]) or 0.0

    ctl, atl = seed, seed
    series = {}
    for day in dates:
        load = loads_by_date[day]
        ctl += (load - ctl) / ctl_days
        atl += (load - atl) / atl_days
        series[day] = {"fitness": round(ctl, 1), "fatigue": round(atl, 1),
                       "form": round(ctl - atl, 1)}
        if day == as_of.isoformat():
            break

    latest = series.get(as_of.isoformat()) or series[sorted(series)[-1]]
    form = latest["form"]

    # Form is judged as a share of fitness, not as an absolute number. The usual
    # published thresholds (+5 to +15 "fresh", -10 to -30 "building") come from
    # TSS, where fitness typically lands between 50 and 100. This pipeline
    # measures load in TRIMP, which has a different scale entirely, so borrowing
    # those cutoffs unchanged would mislabel a normal training week as a crisis.
    # Expressed as a percentage of fitness, the bands hold whatever the units are.
    form_pct = (form / latest["fitness"] * 100) if latest["fitness"] else None

    if form_pct is None:
        zone, label = "unknown", "Not enough load history to judge form."
    elif form_pct >= 15:
        zone, label = "fresh", "Very fresh — fitness well clear of fatigue. Race-ready, or under-trained."
    elif form_pct >= 5:
        zone, label = "tapered", "Fresh and fit — this is the window you want to race in."
    elif form_pct >= -15:
        zone, label = "building", "Productive training: carrying fatigue but absorbing it."
    elif form_pct >= -30:
        zone, label = "loaded", "Deep in the work. Right for a peak week, not for a month."
    else:
        zone, label = "overloaded", "Fatigue is a long way ahead of fitness — a recovery week is overdue."

    return {
        "fitness": latest["fitness"],
        "fatigue": latest["fatigue"],
        "form": form,
        "form_pct": round_or_none(form_pct, 1),
        "zone": zone,
        "label": label,
        "seeded_from": round(seed, 1),
        "series": {k: v for k, v in series.items()
                   if k >= (as_of - timedelta(days=89)).isoformat()},
        "method": f"EWMA, {ctl_days}-day fitness / {atl_days}-day fatigue, seeded from the "
                  f"first {seed_weeks} weeks of available load. Form is judged as a share of "
                  f"fitness so the bands don't depend on the load unit.",
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


def nocturnal_baseline(history: list, as_of: str) -> dict:
    """Tonight's nocturnal heart-rate drop against its own 28-night baseline.

    Same shape as the resting-HR baseline, but the sign is inverted: a *bigger*
    drop is better, so a negative delta is the warning. This series only starts
    where the overnight data starts, so it reports how many nights it actually
    has -- a baseline off four nights is not a baseline, and the dashboard says so.
    """
    prior = [h for h in history if h["date"] < as_of][-28:]
    values = [h.get("nocturnal_drop") for h in prior if h.get("nocturnal_drop") is not None]
    today = next((h.get("nocturnal_drop") for h in history if h["date"] == as_of), None)
    timing = next((h.get("nocturnal_drop_timing") for h in history if h["date"] == as_of), None)

    if not values:
        return {"today": today, "baseline": None, "delta": None, "n": 0, "timing": timing,
                "note": "Overnight heart-rate history starts here — the baseline builds from now."}

    baseline = statistics.median(values)
    stdev = statistics.pstdev(values) if len(values) > 1 else 0.0
    delta = (today - baseline) if today is not None else None

    return {
        "today": today,
        "baseline": round(baseline, 1),
        "delta": round_or_none(delta, 1),
        "stdev": round(stdev, 2),
        "n": len(values),
        "timing": timing,
        "note": None if len(values) >= 14 else
                f"Baseline is still thin ({len(values)} of 28 nights) — treat it loosely.",
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


def _score_nocturnal(delta, baseline):
    """A deeper-than-usual nocturnal drop is good; a shallower one is the warning."""
    if delta is None or baseline in (None, 0):
        return None
    # Scale against the baseline itself: losing half your usual drop is a
    # meaningful signal whether your usual drop is 6 bpm or 14.
    return clamp(0.5 + (delta / max(baseline, 4.0)), 0.0, 1.0)


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


def readiness(rhr: dict, sleep: dict, day: dict, load: dict, weights: dict,
              nocturnal: dict = None) -> dict:
    """A 0-100 composite of the recovery signals this device actually reports.

    Each component scores 0-1, then contributes its configured weight. Missing
    components are dropped and the remaining weights are renormalised, so the
    score is always on a 0-100 scale -- and ``inputs_used`` records which
    signals were really behind it, because a score built from two inputs
    deserves less trust than one built from five.

    This is a heuristic, not a validated physiological model, and the dashboard
    says so.
    """
    nocturnal = nocturnal or {}
    components = {
        "resting_hr": _score_rhr(rhr.get("delta")),
        # The stand-in for HRV: how far heart rate fell overnight, against how
        # far it usually falls.
        "nocturnal_drop": _score_nocturnal(nocturnal.get("delta"), nocturnal.get("baseline")),
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


def fitness_summary(activities: list, as_of: date, match_window_days=120) -> dict:
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
        # Every run in a wider window, so the plan matcher can reconcile the
        # whole block rather than just the last six weeks.
        "all_runs": [
            {"date": r["date"], "miles": r["miles"], "pace": r["pace"], "avg_hr": r["avg_hr"]}
            for r in sorted(
                (run_summary(a) for a in activities
                 if is_run(a)
                 and (store.activity_date(a) or date.min) >= as_of - timedelta(days=match_window_days)
                 and (store.activity_date(a) or date.min) <= as_of),
                key=lambda r: r["date"],
            )
        ],
        "projection": riegel_prediction(runs),
    }


def readiness_series(history: list, loads_by_date: dict, weights: dict,
                     sleep_target: float) -> dict:
    """Readiness recomputed for every day in the history.

    Needed for two things: a trend, and the "three of the last four days below
    55" trigger in the adaptive layer, which can't work off a single day's score.
    Each day is scored using only the data available up to that day, so the
    series is what the dashboard would have said at the time rather than a
    hindsight rescoring.
    """
    out = {}
    for index, day in enumerate(history):
        as_of_iso = day["date"]
        window = history[: index + 1]
        rhr = rhr_baseline(window, as_of_iso)
        nocturnal = nocturnal_baseline(window, as_of_iso)
        sleep = sleep_summary(window, as_of_iso, sleep_target)
        try:
            load = acwr(loads_by_date, datetime.fromisoformat(as_of_iso).date())
        except Exception:
            load = {}
        score = readiness(rhr, sleep, day, load, weights, nocturnal=nocturnal)
        out[as_of_iso] = {"score": score["score"], "band": score["band"]}
    return out


def garmin_metrics(vo2_snapshots: dict, prediction_snapshots: dict) -> dict:
    """Garmin's own modelled numbers, kept as a trend.

    These endpoints hand back only the current value, so the pipeline stores each
    observation and the trend is the store's. Two are genuinely useful:

    VO2 max -- the device's aerobic-capacity estimate, and the direction it is
    moving matters more than the number.

    Race predictions -- Garmin models these from every run on the watch, where
    our Riegel projection extrapolates from one. They disagree, and that is the
    point: showing both brackets the honest range instead of picking whichever
    number flatters the goal.
    """
    vo2_series = {}
    for day, payload in vo2_snapshots.items():
        entries = payload if isinstance(payload, list) else [payload]
        for entry in entries:
            generic = (entry or {}).get("generic") or {}
            value = generic.get("vo2MaxPreciseValue") or generic.get("vo2MaxValue")
            observed = generic.get("calendarDate") or day
            if value:
                vo2_series[observed] = value

    vo2_sorted = sorted(vo2_series.items())
    vo2_trend = None
    if len(vo2_sorted) >= 4:
        half = len(vo2_sorted) // 2
        first = mean([v for _, v in vo2_sorted[:half]])
        second = mean([v for _, v in vo2_sorted[half:]])
        if first:
            vo2_trend = round(second - first, 2)

    latest_prediction = None
    if prediction_snapshots:
        payload = prediction_snapshots[sorted(prediction_snapshots)[-1]]
        if isinstance(payload, dict):
            latest_prediction = {
                "observed": payload.get("calendarDate"),
                "5k": fmt_duration(payload.get("time5K")),
                "10k": fmt_duration(payload.get("time10K")),
                "half": fmt_duration(payload.get("timeHalfMarathon")),
                "marathon": fmt_duration(payload.get("timeMarathon")),
                "marathon_seconds": payload.get("timeMarathon"),
                "marathon_pace": fmt_pace(payload["timeMarathon"] / MARATHON_MILES)
                                 if payload.get("timeMarathon") else None,
            }

    return {
        "vo2_max": vo2_sorted[-1][1] if vo2_sorted else None,
        "vo2_observed": vo2_sorted[-1][0] if vo2_sorted else None,
        "vo2_trend": vo2_trend,
        "vo2_series": dict(vo2_sorted),
        "race_prediction": latest_prediction,
        "note": "VO2 max and race predictions are Garmin's own models, stored each "
                "sync so they build a trend the API won't give you.",
    }


# --------------------------------------------------------------------------
# triathlon: the reference race, and what it implies at full distance
# --------------------------------------------------------------------------

SWIM_TYPES = {"lap_swimming", "open_water_swimming", "swimming"}
BIKE_TYPES = {"cycling", "indoor_cycling", "virtual_ride", "road_biking", "gravel_cycling"}


def reference_race_splits(activities: list, race_date: str) -> dict:
    """Pull the three legs of a completed triathlon out of that day's activities.

    Bike and run classify cleanly by activity type. The swim often doesn't: a
    watch in open water regularly files the leg as something generic, and on this
    race it came back as `indoor_cardio`. Rather than quietly treating any
    leftover session as the swim, the unclassified case is used but flagged, so
    the page can say the classification was inferred.
    """
    legs = {"swim": None, "bike": None, "run": None}
    leftovers = []

    for activity in activities:
        day = store.activity_date(activity)
        if not day or day.isoformat() != race_date:
            continue
        type_key = (activity.get("activityType") or {}).get("typeKey") or ""
        summary = {
            "type": type_key,
            "name": activity.get("activityName"),
            "minutes": round((activity.get("duration") or 0) / 60, 1),
            "miles": round((activity.get("distance") or 0) / METERS_PER_MILE, 2),
            "avg_hr": activity.get("averageHR"),
            "inferred": False,
        }
        if type_key in SWIM_TYPES:
            legs["swim"] = summary
        elif type_key in BIKE_TYPES:
            legs["bike"] = summary
        elif "running" in type_key:
            legs["run"] = summary
        else:
            leftovers.append(summary)

    # Longest unclassified session stands in for an unrecorded swim leg.
    if legs["swim"] is None and leftovers:
        candidate = max(leftovers, key=lambda s: s["minutes"])
        candidate["inferred"] = True
        legs["swim"] = candidate

    recorded = [leg for leg in legs.values() if leg]
    total_minutes = sum(leg["minutes"] for leg in recorded) if recorded else None

    return {
        "date": race_date,
        "legs": legs,
        "moving_minutes": round(total_minutes, 1) if total_minutes else None,
        "moving_time": fmt_duration(total_minutes * 60) if total_minutes else None,
        "complete": all(legs.values()),
        "note": "Swim leg classification was inferred from an unclassified session."
                if (legs["swim"] or {}).get("inferred") else None,
    }


# How each leg slows when the distance doubles. These are the conventional
# planning factors, and they are assumptions rather than measurements -- stated
# here so they can be argued with rather than buried in an arithmetic expression.
IRONMAN_FADE = {
    "swim": (1.02, 1.10),   # least affected: it's short and mostly aerobic either way
    "bike": (1.06, 1.14),   # ridden at lower intensity to protect the run
    "run":  (1.12, 1.28),   # the marathon off the bike is where a full distance is won or lost
}


def ironman_projection(reference: dict, config: dict) -> dict:
    """Extrapolate a full-distance time from a completed 70.3.

    Each leg's pace from the half is slowed by a fade factor and applied over the
    full distance. A range is reported rather than a single time, because a first
    full distance has genuine spread and a single number would imply a precision
    that isn't there.

    This is an extrapolation from one race, not a prediction. It is labelled as
    such everywhere it appears.
    """
    legs = reference.get("legs") or {}
    if not reference.get("complete"):
        return {"available": False,
                "note": "Need all three legs of a completed triathlon to extrapolate from."}

    targets = {
        "swim": config.get("swim_miles", 2.4),
        "bike": config.get("bike_miles", 112.0),
        "run": config.get("run_miles", MARATHON_MILES),
    }
    reference_distance = {
        "swim": legs["swim"]["miles"] or 1.2,   # swim distance is often unrecorded
        "bike": legs["bike"]["miles"],
        "run": legs["run"]["miles"],
    }
    if not reference_distance["swim"]:
        reference_distance["swim"] = 1.2

    rows, fast_total, slow_total = [], 0.0, 0.0
    for leg in ("swim", "bike", "run"):
        source = legs[leg]
        distance = reference_distance[leg] or 1
        pace_per_mile = source["minutes"] / distance
        fast_fade, slow_fade = IRONMAN_FADE[leg]
        fast = pace_per_mile * fast_fade * targets[leg]
        slow = pace_per_mile * slow_fade * targets[leg]
        fast_total += fast
        slow_total += slow
        rows.append({
            "leg": leg,
            "reference_time": fmt_duration(source["minutes"] * 60),
            "reference_miles": round(distance, 2),
            "reference_pace_min_per_mile": round(pace_per_mile, 2),
            "reference_speed_mph": round(60 / pace_per_mile, 1) if pace_per_mile else None,
            "target_miles": targets[leg],
            "fade": [fast_fade, slow_fade],
            "fast": fmt_duration(fast * 60),
            "slow": fmt_duration(slow * 60),
            "fast_minutes": round(fast, 1),
            "slow_minutes": round(slow, 1),
        })

    transition = config.get("transition_minutes", 15)
    fast_total += transition
    slow_total += transition
    midpoint = (fast_total + slow_total) / 2

    goal_seconds = None
    goal_time = config.get("goal_time")
    if goal_time:
        try:
            h, m, s = (int(x) for x in str(goal_time).split(":"))
            goal_seconds = h * 3600 + m * 60 + s
        except Exception:
            goal_seconds = None

    return {
        "available": True,
        "legs": rows,
        "transition_minutes": transition,
        "fast": fmt_duration(fast_total * 60),
        "slow": fmt_duration(slow_total * 60),
        "midpoint": fmt_duration(midpoint * 60),
        "midpoint_seconds": round(midpoint * 60),
        "goal": goal_time,
        "goal_seconds": goal_seconds,
        "on_track": (goal_seconds is not None and midpoint * 60 <= goal_seconds),
        "note": "Extrapolated from one completed 70.3 by slowing each leg's pace and "
                "applying it over the full distance, plus "
                f"{transition} minutes of transitions. Conventional fade factors, not "
                "measurements — a first full distance has real spread, which is why this "
                "is a range.",
    }


def triathlon_insights(reference: dict, projection: dict, activities: list, as_of: date) -> list:
    """Observations that follow from the data, rather than generic race advice."""
    out = []
    legs = reference.get("legs") or {}
    if not projection.get("available"):
        return out

    by_leg = {row["leg"]: row for row in projection["legs"]}

    run = by_leg.get("run")
    if run:
        out.append(
            f"The run is where the time is. Your 70.3 half went {fmt_pace(run['reference_pace_min_per_mile'] * 60)}/mi "
            f"off the bike; at full distance the same fitness projects "
            f"{run['fast']} to {run['slow']} for the marathon. Every minute per mile "
            "you take off standalone marathon pace is roughly 26 minutes here.")

    bike = by_leg.get("bike")
    if bike and bike.get("reference_speed_mph"):
        out.append(
            f"You rode the 70.3 at {bike['reference_speed_mph']} mph. Held at full-distance "
            f"intensity that is {bike['fast']} to {bike['slow']} for 112 miles — the single "
            "largest block of the day, and the one that decides what is left for the run.")

    # Swim and bike volume in the last 8 weeks, from real activities.
    window_start = as_of - timedelta(days=56)
    swim_minutes = bike_minutes = 0.0
    for activity in activities:
        day = store.activity_date(activity)
        if not day or day < window_start or day > as_of:
            continue
        type_key = (activity.get("activityType") or {}).get("typeKey") or ""
        if type_key in SWIM_TYPES:
            swim_minutes += (activity.get("duration") or 0) / 60
        elif type_key in BIKE_TYPES:
            bike_minutes += (activity.get("duration") or 0) / 60

    out.append(
        f"Last 8 weeks: {round(swim_minutes)} minutes swimming and {round(bike_minutes)} minutes "
        "riding. The marathon block is deliberately run-only, so both are near zero by design — "
        "worth knowing rather than worrying about, but the clock on rebuilding them starts "
        "the day after the marathon.")

    if (legs.get("swim") or {}).get("inferred"):
        out.append(
            "The swim leg was not recorded as a swim activity, so its split is taken from the "
            "unclassified session on race day. Treat the swim numbers as approximate.")

    return out


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

def energy_baseline(history: list, days: dict) -> dict:
    """Maintenance calories, measured rather than estimated.

    Garmin's daily summary reports ``bmrKilocalories`` and ``activeKilocalories``,
    and their sum is measured total daily energy expenditure. That is a far better
    basis for a calorie target than a Mifflin-St Jeor estimate, which would need
    height and would ignore how much the athlete actually moved.

    One catch: the current day is partial. Its BMR figure is only accrued to the
    present moment, so today's total is always an undercount and must be excluded.
    A day is treated as complete when its BMR is at least 90% of the typical BMR
    seen across the window.
    """
    rows = []
    for day_str, payload in days.items():
        stats = (payload or {}).get("stats") or {}
        bmr, active = stats.get("bmrKilocalories"), stats.get("activeKilocalories")
        total = stats.get("totalKilocalories")
        if bmr and total:
            rows.append({"date": day_str, "bmr": bmr, "active": active or 0, "total": total})
    if not rows:
        return {"maintenance": None, "bmr": None, "days_used": 0,
                "note": "Garmin has not reported calorie data for any stored day."}

    rows.sort(key=lambda r: r["date"])
    typical_bmr = median([r["bmr"] for r in rows])
    complete = [r for r in rows if r["bmr"] >= typical_bmr * 0.9]
    recent = complete[-14:]
    if not recent:
        return {"maintenance": None, "bmr": round(typical_bmr), "days_used": 0,
                "note": "No complete day of calorie data yet — today is still accruing."}

    maintenance = mean([r["total"] for r in recent])
    return {
        "maintenance": round(maintenance),
        "bmr": round(typical_bmr),
        "avg_active": round(mean([r["active"] for r in recent])),
        "days_used": len(recent),
        "range": [round(min(r["total"] for r in recent)), round(max(r["total"] for r in recent))],
        "note": f"Measured from Garmin: BMR plus active calories, averaged over the last "
                f"{len(recent)} complete days. Today is excluded because its BMR is still accruing.",
    }


NUTRITION_BANDS = {
    "race": "big", "long": "big",
    "quality": "quality", "medium_long": "quality",
    "easy": "easy",
    "cross": "recovery", "rest": "recovery",
}

BAND_INTENT = {
    "big": "High carb, low fat, calories at maintenance. Today's session needs fuelling, "
           "so this is not the day to run a deficit.",
    "quality": "High carb, moderate fat, a small deficit. Enough carbohydrate to hit the "
               "paces without giving up the deficit entirely.",
    "easy": "Moderate carb, higher fat, a real deficit. Aerobic work runs fine on less "
            "carbohydrate.",
    "recovery": "Lower carb, highest fat, the largest deficit. Nothing today needs "
                "glycogen, so this is where the weekly deficit comes from.",
}


def nutrition(plan_entry: dict, energy: dict, config: dict) -> dict:
    """Today's calorie and macro targets, built in a fixed order.

    1. Protein is held flat at ``protein_per_lb`` regardless of the day, because
       the point of it is preserving muscle in a deficit.
    2. Fat gets a floor in g/lb that varies by day type, so it never drops to a
       level that costs you hormonally just to make room for carbohydrate.
    3. Carbohydrate takes every calorie left over.

    That ordering is what makes the periodisation fall out automatically: hard days
    have a bigger calorie budget and a lower fat floor, so carbohydrate absorbs
    the difference. No separate carb rule is needed.
    """
    weight_lb = config.get("weight_lb")
    protein_per_lb = config.get("protein_per_lb", 1.0)
    maintenance = energy.get("maintenance")

    kind = (plan_entry or {}).get("kind") or "easy"
    miles = (plan_entry or {}).get("miles") or 0
    band = NUTRITION_BANDS.get(kind, "easy")
    # A genuinely long run earns the big-day treatment even if the plan calls it
    # something else.
    if miles >= 13:
        band = "big"

    deficits = config.get("deficit_by_day") or {}
    fat_floors = config.get("fat_g_per_lb_by_day") or {}
    adjustment = deficits.get(band, -300)
    fat_per_lb = fat_floors.get(band, 0.45)

    if not weight_lb or not maintenance:
        return {
            "band": band, "session_kind": kind, "planned_miles": miles,
            "intent": BAND_INTENT.get(band),
            "maintenance_kcal": maintenance, "target_kcal": None,
            "protein_g": round(weight_lb * protein_per_lb) if weight_lb else None,
            "carbs_g": None, "fat_g": None, "weight_lb": weight_lb,
            "energy": energy,
            "note": "Set weight_lb in config.json, and let the sync collect a complete day "
                    "of Garmin calorie data, for gram targets.",
        }

    protein_g = round(weight_lb * protein_per_lb)
    target_kcal = round(maintenance + adjustment)
    fat_g = round(weight_lb * fat_per_lb)

    protein_kcal = protein_g * 4
    fat_kcal = fat_g * 9
    carb_kcal = max(target_kcal - protein_kcal - fat_kcal, 0)
    carbs_g = round(carb_kcal / 4)

    weight_kg = weight_lb / 2.20462
    return {
        "band": band,
        "session_kind": kind,
        "planned_miles": miles,
        "intent": BAND_INTENT.get(band),
        "maintenance_kcal": maintenance,
        "adjustment_kcal": adjustment,
        "target_kcal": target_kcal,
        "protein_g": protein_g,
        "carbs_g": carbs_g,
        "fat_g": fat_g,
        "split_pct": {
            "protein": round(100 * protein_kcal / target_kcal),
            "carbs": round(100 * carb_kcal / target_kcal),
            "fat": round(100 * fat_kcal / target_kcal),
        },
        "per_kg": {
            "protein": round(protein_g / weight_kg, 1),
            "carbs": round(carbs_g / weight_kg, 1),
            "fat": round(fat_g / weight_kg, 1),
        },
        "weight_lb": weight_lb,
        "protein_per_lb": protein_per_lb,
        "energy": energy,
        "note": "Protein held flat at "
                f"{protein_per_lb:g}g per lb. Fat floored by day type so it never drops too "
                "low; carbohydrate takes the remaining calories, which is what makes hard "
                "days high-carb and easy days higher-fat. Intake is not tracked — Garmin "
                "carries no food log — so these are targets, not a record of what you ate.",
    }


def nutrition_week(plan_days: dict, week_start: str, energy: dict, config: dict) -> list:
    """The week's nutrition pattern, so the periodisation is visible at a glance."""
    out = []
    for offset in range(7):
        try:
            day = (datetime.fromisoformat(week_start).date() + timedelta(days=offset)).isoformat()
        except Exception:
            continue
        entry = plan_days.get(day)
        if not entry:
            continue
        target = nutrition(entry, energy, config)
        out.append({
            "date": day, "band": target["band"], "kind": entry.get("kind"),
            "miles": entry.get("miles"),
            "kcal": target["target_kcal"], "carbs_g": target["carbs_g"],
            "protein_g": target["protein_g"], "fat_g": target["fat_g"],
        })
    return out


# --------------------------------------------------------------------------
# assembly
# --------------------------------------------------------------------------

def build(as_of: date = None, days=None, activities=None, plan=None, config=None,
          snapshots=None, manual=None) -> dict:
    """Compute every signal. Every input can be injected.

    Nothing here reaches for the store unless it wasn't given the data, which is
    what lets the tests run on synthetic fixtures and the demo build run on a
    fabricated athlete without any real data leaking into it.
    """
    as_of = as_of or date.today()
    days = days if days is not None else store.all_days()
    activities = activities if activities is not None else store.all_activities()
    plan = plan if plan is not None else load_json(PLAN_JSON, {"days": {}, "race": {}})
    config = config if config is not None else load_json(CONFIG_JSON, {})
    if snapshots is None:
        # Fitness age is deliberately not consumed: it's a soft Garmin guess with
        # no bearing on any decision, and showing it invites arguing with it.
        snapshots = {name: store.snapshots(name)
                     for name in ("max_metrics", "race_predictions")}

    weights = config.get("readiness_weights") or {
        "resting_hr": 25, "nocturnal_drop": 20, "sleep": 25,
        "body_battery": 15, "stress": 5, "training_load": 10}
    sleep_target = config.get("sleep_target_hours") or 7.5

    manual_entries = manual if manual is not None else store.all_manual()
    history = [extract_day_metrics(d, days[d], manual_entries.get(d))
               for d in sorted(days.keys())]
    as_of_iso = as_of.isoformat()

    # The most recent day we actually hold data for -- today's numbers may not
    # have synced yet, and showing yesterday honestly beats showing blanks.
    latest = next((h for h in reversed(history) if h["date"] <= as_of_iso), None) or \
        extract_day_metrics(as_of_iso, {})

    hr_max_info = estimate_hr_max(activities, config.get("hr_max"))
    rhr_info = rhr_baseline(history, latest["date"])
    resting_for_trimp = rhr_info.get("baseline") or latest.get("rhr") or 55

    # Measure this athlete's own load-per-minute per sport before computing any
    # load, so HR-less sessions are scored against his sessions and not a prior.
    rate_info = sport_load_rates(activities, hr_max_info["value"], resting_for_trimp)

    # The load window starts where the data starts -- never earlier. Padding it
    # back to a fixed 120 days fills the gap before the watch existed with zeros,
    # and since the fitness curve is seeded from the earliest weeks it then seeds
    # from nothing and spends months catching up to a fitness he already had.
    # That understates fitness and so overstates fatigue for the whole series.
    first_dates = []
    if history:
        first_dates.append(datetime.fromisoformat(history[0]["date"]).date())
    activity_dates = [d for d in (store.activity_date(a) for a in activities) if d]
    if activity_dates:
        first_dates.append(min(activity_dates))
    load_start = min(first_dates) if first_dates else as_of - timedelta(days=41)
    loads = daily_loads(activities, hr_max_info["value"], resting_for_trimp,
                        load_start, as_of, rates=rate_info["rates"])
    load_info = acwr(loads, as_of)
    form_info = fitness_fatigue_form(loads, as_of)
    sleep_info = sleep_summary(history, latest["date"], sleep_target)
    nocturnal_info = nocturnal_baseline(history, latest["date"])
    readiness_info = readiness(rhr_info, sleep_info, latest, load_info, weights,
                               nocturnal=nocturnal_info)
    garmin_info = garmin_metrics(snapshots.get("max_metrics") or {},
                                 snapshots.get("race_predictions") or {})
    fitness = fitness_summary(activities, as_of)
    adherence_info = adherence(plan, activities, as_of)

    readiness_by_day = readiness_series(history, loads, weights, sleep_target)
    for day in history:
        entry = readiness_by_day.get(day["date"]) or {}
        day["readiness_score"] = entry.get("score")
        day["readiness_band"] = entry.get("band")

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
    energy_info = energy_baseline(history, days)

    reference_config = config.get("reference_race") or {}
    reference = reference_race_splits(activities, reference_config.get("date") or "")
    ironman_config = config.get("ironman_2027") or {}
    ironman = ironman_projection(reference, ironman_config)
    ironman_days_out = None
    if ironman_config.get("date"):
        try:
            ironman_days_out = (datetime.fromisoformat(ironman_config["date"]).date() - as_of).days
        except Exception:
            pass

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
            "sleep_gaps": sleep_gaps(history, as_of),
            "unavailable_signals": _unavailable(history),
            "available_signals": _available(history),
            "schema_version": 2,
        },
        "race": {**race, "days_to_race": days_to_race},
        "paces": plan.get("paces", {}),
        "block": plan.get("block", {}),
        # Passed through from the plan file so the page can show the lifting
        # templates and the plan's own rules rather than paraphrasing them.
        "lifting": plan.get("lifting", {}),
        "plan_rules": plan.get("rules", []),
        "athlete": plan.get("athlete", {}),
        "today": {
            "date": as_of_iso,
            "plan": today_entry,
            "week": current_week,
        },
        "readiness": readiness_info,
        "recovery": {
            "latest": latest,
            "resting_hr": rhr_info,
            "nocturnal": nocturnal_info,
            "sleep": sleep_info,
            # 90 days emitted so the page's 30/90-day toggle has something to
            # switch to. The page trims; the pipeline doesn't decide the window.
            "history": history[-90:],
            "readiness_series": {k: v for k, v in readiness_by_day.items()
                                 if k >= (as_of - timedelta(days=89)).isoformat()},
        },
        "load": {
            **load_info,
            "daily": {k: v for k, v in sorted(loads.items())
                      if k >= (as_of - timedelta(days=89)).isoformat()},
            "method": "Banister TRIMP from average HR where available, per-sport rate otherwise.",
            "measured_sport_rates": rate_info["measured"],
        },
        "form": form_info,
        "garmin": garmin_info,
        "fitness": fitness,
        "projection": _projection(fitness["projection"], garmin_info, race, goal_seconds),
        "adherence": adherence_info,
        # Dates with any recorded activity, so the plan view can mark a session
        # done without the browser re-deriving it from the activity list.
        "completed_days": sorted(
            {d.isoformat() for d in (store.activity_date(a) for a in activities) if d}
        ),
        "triathlon": {
            "reference_race": {**reference_config, **reference},
            "ironman": {**ironman, "name": ironman_config.get("name"),
                        "date": ironman_config.get("date"),
                        "days_out": ironman_days_out},
            "insights": triathlon_insights(reference, ironman, activities, as_of),
        },
        "nutrition": nutrition(today_entry, energy_info, config),
        "nutrition_week": nutrition_week(plan.get("days", {}),
                                        (current_week or {}).get("start") or as_of_iso,
                                        energy_info, config),
        "plan_days": plan.get("days", {}),
    }


def sleep_gaps(history: list, as_of: date, window_days=30) -> dict:
    """Nights in the recent window with no sleep figure at all.

    Worth surfacing rather than quietly averaging over: sleep and the nocturnal
    drop together carry 45% of the readiness weight, so a missing night doesn't
    just leave a hole in one chart -- it changes what the readiness score is
    actually made of.
    """
    start = (as_of - timedelta(days=window_days)).isoformat()
    recent = [h for h in history if start <= h["date"] <= as_of.isoformat()]

    missing = [h["date"] for h in recent if h["sleep_hours"] is None]
    filled = [{"date": h["date"], "hours": h["sleep_hours"], "source": h["sleep_source"]}
              for h in recent if h["sleep_source"] and h["sleep_source"] != "garmin"]

    return {
        "window_days": window_days,
        "nights_in_window": len(recent),
        "missing": missing,
        "missing_count": len(missing),
        "filled_by_hand": filled,
        # Last night specifically, because that's the one worth prompting about.
        "last_night_missing": bool(recent and recent[-1]["sleep_hours"] is None
                                   and recent[-1]["date"] == as_of.isoformat()),
        "note": "A night the watch missed can be filled in with "
                "`python3 log_sleep.py <date> <hours>`. Sleep duration is recoverable "
                "that way; the overnight heart-rate series is not, so the nocturnal "
                "drop stays unavailable for those nights.",
    }


def _available(history: list) -> list:
    """Signals that are present and doing work, stated so the page can say so.

    Worth surfacing alongside the unavailable list: the interesting story is not
    just that HRV is missing, it's what is being used in its place.
    """
    recent = history[-14:]
    out = []
    nights = sum(1 for h in recent if h.get("overnight_samples"))
    if nights:
        out.append({"signal": "Overnight heart-rate series",
                    "reason": f"In use on {nights} of the last {len(recent)} nights — "
                              "roughly one sample every two minutes. This is where the "
                              "nocturnal-drop signal comes from, standing in for HRV."})
    if any(h.get("deep_pct") for h in recent):
        out.append({"signal": "Sleep stages",
                    "reason": "Deep, light, REM and awake durations are all reported, so sleep "
                              "is scored here from duration and composition rather than from "
                              "Garmin's missing sleep score."})
    return out


def _projection(riegel: dict, garmin_info: dict, race: dict, goal_seconds) -> dict:
    """Bracket the goal with both models rather than picking one.

    Riegel extrapolates one training run and reads pessimistic, because it
    assumes the input was a maximal effort. Garmin models every run on the watch
    and tends to read optimistic. The truth is usually between them, and showing
    the bracket is more honest than showing whichever number is kinder.
    """
    garmin_prediction = garmin_info.get("race_prediction") or {}
    garmin_seconds = garmin_prediction.get("marathon_seconds")
    riegel_seconds = riegel.get("predicted_seconds")

    candidates = [s for s in (garmin_seconds, riegel_seconds) if s]
    best = min(candidates) if candidates else None
    gap = (best - goal_seconds) if (best and goal_seconds) else None

    out = {
        **riegel,
        "goal_seconds": goal_seconds,
        "goal": race.get("goal"),
        "garmin": garmin_prediction or None,
        "best_seconds": best,
        "best": fmt_duration(best) if best else None,
        "gap_seconds": gap,
        "gap": (("+" if gap > 0 else "−") + fmt_duration(abs(gap))) if gap else None,
        "on_track": (gap is not None and gap <= 0),
    }
    if garmin_seconds and riegel_seconds:
        low, high = sorted((garmin_seconds, riegel_seconds))
        out["bracket"] = [fmt_duration(low), fmt_duration(high)]
        out["bracket_note"] = (
            f"Garmin's model says {fmt_duration(garmin_seconds)}; extrapolating your training "
            f"runs says {fmt_duration(riegel_seconds)}. Garmin models every run and reads "
            "optimistic; Riegel assumes a maximal effort it never got and reads pessimistic. "
            "The real number is somewhere in between."
        )
    return out


def _unavailable(history: list) -> list:
    """Signals the schema supports but this device never actually fills in."""
    recent = history[-14:]
    out = []
    if recent and not any(h["hrv_last_night"] for h in recent):
        out.append({"signal": "HRV", "reason":
                    "Both get_hrv_data and get_hrv_data_range return empty for this account — "
                    "this watch does not record it. The nocturnal heart-rate drop, derived from "
                    "the overnight series, stands in for it."})

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
        f_ = signals.get("form", {})
        print(f"  form           fitness {f_.get('fitness')} / fatigue {f_.get('fatigue')} "
              f"= form {f_.get('form')} [{f_.get('zone')}]")
        n_ = signals["recovery"]["nocturnal"]
        print(f"  nocturnal drop {n_.get('today')} vs baseline {n_.get('baseline')} (n={n_.get('n')})")
        g_ = signals.get("garmin", {})
        print(f"  VO2 max        {g_.get('vo2_max')} (trend {g_.get('vo2_trend')})")
        if g_.get("race_prediction"):
            print(f"  Garmin says    {g_['race_prediction'].get('marathon')} marathon")
        tri = signals.get("triathlon", {})
        ref, im = tri.get("reference_race", {}), tri.get("ironman", {})
        if ref.get("moving_time"):
            print(f"  70.3 reference {ref['moving_time']} moving ({ref.get('date')})")
        if im.get("available"):
            print(f"  Ironman 140.6  {im['fast']} – {im['slow']} (mid {im['midpoint']})")
        print(f"  28d run miles  {signals['fitness']['miles_28day']} (longest {signals['fitness']['longest_run_28day']})")
        print(f"  projection     {p.get('predicted')} vs goal {p.get('goal')} (gap {p.get('gap')})")
        print(f"  adherence      {signals['adherence']['key_sessions_done_to_date']}/"
              f"{signals['adherence']['key_sessions_due_to_date']} key sessions")
        print(f"  race in        {signals['race'].get('days_to_race')} days")
        for item in signals["meta"]["unavailable_signals"]:
            print(f"  unavailable: {item['signal']}")


if __name__ == "__main__":
    main()
