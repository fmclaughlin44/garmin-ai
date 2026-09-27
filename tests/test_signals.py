"""
Tests for the signal layer.

These matter more than they might look. Every number the dashboard shows is
derived here, and the derivations have edge cases that are easy to get quietly
wrong: a workload ratio computed against 3 days of history would look perfectly
plausible and mean nothing; a readiness score that silently treats a missing
input as zero would report "poor recovery" for a day the watch simply didn't
sync. Each test below pins down one of those behaviours.
"""

import math
import sys
from datetime import date, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import signals  # noqa: E402


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def make_activity(day, minutes, avg_hr=None, type_key="running", miles=None, activity_id=1):
    payload = {
        "activityId": activity_id,
        "startTimeLocal": f"{day} 08:00:00",
        "duration": minutes * 60,
        "activityType": {"typeKey": type_key},
    }
    if avg_hr is not None:
        payload["averageHR"] = avg_hr
    if miles is not None:
        payload["distance"] = miles * signals.METERS_PER_MILE
    return payload


def make_day(day, rhr=None, sleep_hours=None, stress=None, bb_wake=None):
    stats = {}
    if rhr is not None:
        stats["restingHeartRate"] = rhr
    if stress is not None:
        stats["averageStressLevel"] = stress
    if bb_wake is not None:
        stats["bodyBatteryAtWakeTime"] = bb_wake
    payload = {"stats": stats}
    if sleep_hours is not None:
        payload["sleep"] = {"dailySleepDTO": {
            "sleepTimeSeconds": int(sleep_hours * 3600),
            "deepSleepSeconds": int(sleep_hours * 3600 * 0.2),
            "remSleepSeconds": int(sleep_hours * 3600 * 0.22),
        }}
    return payload


# --------------------------------------------------------------------------
# formatting
# --------------------------------------------------------------------------

def test_fmt_pace_rounds_to_mm_ss():
    assert signals.fmt_pace(515) == "8:35"
    assert signals.fmt_pace(600) == "10:00"
    assert signals.fmt_pace(605.4) == "10:05"


def test_fmt_pace_rejects_nonsense():
    assert signals.fmt_pace(None) is None
    assert signals.fmt_pace(0) is None
    assert signals.fmt_pace(-10) is None
    assert signals.fmt_pace(math.inf) is None


def test_fmt_duration():
    assert signals.fmt_duration(3 * 3600 + 45 * 60) == "3:45:00"
    assert signals.fmt_duration(59) == "0:00:59"


# --------------------------------------------------------------------------
# training load
# --------------------------------------------------------------------------

def test_trimp_is_exponential_in_intensity():
    """Twice the intensity must cost much more than twice the load.

    This is the whole reason for using TRIMP over duration: an hour of hard
    running and an hour of jogging must not score the same.
    """
    easy = signals.activity_load(make_activity("2026-09-01", 60, avg_hr=130), 190, 50)
    hard = signals.activity_load(make_activity("2026-09-01", 60, avg_hr=170), 190, 50)
    assert hard["load"] > easy["load"] * 2
    assert not easy["estimated"] and not hard["estimated"]


def test_trimp_scales_linearly_with_duration():
    short = signals.activity_load(make_activity("2026-09-01", 30, avg_hr=150), 190, 50)
    long = signals.activity_load(make_activity("2026-09-01", 60, avg_hr=150), 190, 50)
    assert long["load"] == pytest.approx(short["load"] * 2, rel=0.01)


def test_load_falls_back_to_sport_rate_without_hr():
    """A session with no HR still has to produce a load, clearly flagged."""
    result = signals.activity_load(make_activity("2026-09-01", 60, type_key="cycling"), 190, 50)
    assert result["estimated"] is True
    assert result["load"] == pytest.approx(60 * signals.SPORT_LOAD_PER_MIN["cycling"])


def test_load_clamps_hr_below_resting():
    """A bad HR reading under resting must not produce a negative load."""
    result = signals.activity_load(make_activity("2026-09-01", 60, avg_hr=40), 190, 50)
    assert result["load"] >= 0


def test_zero_duration_is_zero_load():
    assert signals.activity_load(make_activity("2026-09-01", 0, avg_hr=150), 190, 50)["load"] == 0.0


def test_daily_loads_include_rest_days_as_zero():
    """Rest days are real data -- a sparse dict would break monotony."""
    loads = signals.daily_loads(
        [make_activity("2026-09-03", 60, avg_hr=150)], 190, 50,
        date(2026, 9, 1), date(2026, 9, 5))
    assert set(loads) == {"2026-09-01", "2026-09-02", "2026-09-03", "2026-09-04", "2026-09-05"}
    assert loads["2026-09-01"] == 0.0
    assert loads["2026-09-03"] > 0


def test_daily_loads_sum_multiple_sessions_on_one_day():
    loads = signals.daily_loads(
        [make_activity("2026-09-03", 60, avg_hr=150, activity_id=1),
         make_activity("2026-09-03", 30, avg_hr=150, activity_id=2)],
        190, 50, date(2026, 9, 3), date(2026, 9, 3))
    single = signals.activity_load(make_activity("2026-09-03", 60, avg_hr=150), 190, 50)["load"]
    assert loads["2026-09-03"] == pytest.approx(single * 1.5, rel=0.02)


# --------------------------------------------------------------------------
# ACWR
# --------------------------------------------------------------------------

def test_acwr_refuses_to_guess_without_enough_history():
    """The important one: a ratio off 5 days of data would look real and be junk."""
    as_of = date(2026, 9, 28)
    loads = {(as_of - timedelta(days=i)).isoformat(): 50.0 for i in range(5)}
    result = signals.acwr(loads, as_of)
    assert result["ratio"] is None
    assert result["zone"] == "insufficient_history"
    assert result["days_of_history"] == 5


def test_acwr_is_one_for_perfectly_steady_load():
    as_of = date(2026, 9, 28)
    loads = {(as_of - timedelta(days=i)).isoformat(): 50.0 for i in range(28)}
    result = signals.acwr(loads, as_of)
    assert result["ratio"] == pytest.approx(1.0, abs=0.01)
    assert result["zone"] == "optimal"


def test_acwr_flags_a_spike_as_high_risk():
    as_of = date(2026, 9, 28)
    loads = {(as_of - timedelta(days=i)).isoformat(): 20.0 for i in range(28)}
    for i in range(7):  # triple the last week
        loads[(as_of - timedelta(days=i)).isoformat()] = 60.0
    result = signals.acwr(loads, as_of)
    assert result["ratio"] > 1.5
    assert result["zone"] == "high_risk"


def test_acwr_flags_detraining_after_a_layoff():
    as_of = date(2026, 9, 28)
    loads = {(as_of - timedelta(days=i)).isoformat(): 60.0 for i in range(28)}
    for i in range(7):  # easy week
        loads[(as_of - timedelta(days=i)).isoformat()] = 15.0
    result = signals.acwr(loads, as_of)
    assert result["ratio"] < 0.8
    assert result["zone"] == "detraining"


def test_monotony_is_higher_when_every_day_looks_the_same():
    as_of = date(2026, 9, 28)
    flat = {(as_of - timedelta(days=i)).isoformat(): 40.0 for i in range(28)}
    varied = dict(flat)
    for i, value in enumerate([0, 80, 10, 90, 0, 60, 40]):
        varied[(as_of - timedelta(days=i)).isoformat()] = float(value)
    assert signals.acwr(flat, as_of)["monotony"] is None or \
        signals.acwr(flat, as_of)["monotony"] > signals.acwr(varied, as_of)["monotony"]


# --------------------------------------------------------------------------
# resting HR baseline
# --------------------------------------------------------------------------

def test_rhr_baseline_uses_median_and_ignores_today():
    history = [{"date": f"2026-09-{d:02d}", "rhr": 50} for d in range(1, 21)]
    history.append({"date": "2026-09-21", "rhr": 60})
    result = signals.rhr_baseline(history, "2026-09-21")
    assert result["baseline"] == 50
    assert result["delta"] == 10
    assert result["n"] == 20


def test_rhr_baseline_median_resists_one_outlier():
    """A single sensor artifact must not move the baseline it's judged against."""
    history = [{"date": f"2026-09-{d:02d}", "rhr": 50} for d in range(1, 15)]
    history[3]["rhr"] = 95  # artifact
    history.append({"date": "2026-09-20", "rhr": 52})
    result = signals.rhr_baseline(history, "2026-09-20")
    assert result["baseline"] == 50
    assert result["delta"] == 2


def test_rhr_baseline_handles_no_history():
    result = signals.rhr_baseline([{"date": "2026-09-01", "rhr": 50}], "2026-09-01")
    assert result["baseline"] is None
    assert result["n"] == 0


# --------------------------------------------------------------------------
# sleep
# --------------------------------------------------------------------------

def test_sleep_debt_accumulates_against_target():
    history = [{"date": f"2026-09-{d:02d}", "sleep_hours": 6.5, "sleep_score": None,
                "deep_pct": None, "rem_pct": None} for d in range(1, 8)]
    result = signals.sleep_summary(history, "2026-09-07", target_hours=7.5)
    assert result["nights_recorded"] == 7
    assert result["debt_7day_hours"] == pytest.approx(7.0, abs=0.01)
    assert result["avg_7day_hours"] == pytest.approx(6.5)


def test_sleep_summary_survives_missing_nights():
    history = [{"date": "2026-09-01", "sleep_hours": 8.0, "sleep_score": None,
                "deep_pct": None, "rem_pct": None},
               {"date": "2026-09-02", "sleep_hours": None, "sleep_score": None,
                "deep_pct": None, "rem_pct": None}]
    result = signals.sleep_summary(history, "2026-09-02", target_hours=7.5)
    assert result["nights_recorded"] == 1
    assert result["last_night_hours"] is None


# --------------------------------------------------------------------------
# readiness
# --------------------------------------------------------------------------

WEIGHTS = {"resting_hr": 30, "sleep": 30, "body_battery": 15, "stress": 10, "training_load": 15}


def test_readiness_scores_a_good_day_high():
    result = signals.readiness(
        rhr={"delta": -3}, sleep={"last_night_hours": 8.2, "target_hours": 7.5},
        day={"bb_wake": 90, "stress_avg": 20}, load={"ratio": 1.0}, weights=WEIGHTS)
    assert result["score"] >= 90
    assert result["band"] == "good"
    assert len(result["inputs_used"]) == 5


def test_readiness_scores_a_bad_day_low():
    result = signals.readiness(
        rhr={"delta": 8}, sleep={"last_night_hours": 4.5, "target_hours": 7.5},
        day={"bb_wake": 15, "stress_avg": 70}, load={"ratio": 1.8}, weights=WEIGHTS)
    assert result["score"] <= 25
    assert result["band"] == "poor"


def test_readiness_renormalises_instead_of_penalising_missing_inputs():
    """The important one: a missing signal must not read as a zero signal.

    A day where only RHR and sleep synced, both excellent, has to score high --
    not 60% of high because three inputs were absent.
    """
    full = signals.readiness(
        rhr={"delta": -3}, sleep={"last_night_hours": 8.2, "target_hours": 7.5},
        day={"bb_wake": 90, "stress_avg": 20}, load={"ratio": 1.0}, weights=WEIGHTS)
    partial = signals.readiness(
        rhr={"delta": -3}, sleep={"last_night_hours": 8.2, "target_hours": 7.5},
        day={}, load={}, weights=WEIGHTS)
    assert partial["score"] == pytest.approx(full["score"], abs=2)
    assert partial["inputs_used"] == ["resting_hr", "sleep"]
    assert set(partial["inputs_missing"]) == {"nocturnal_drop", "body_battery",
                                              "stress", "training_load"}


def test_readiness_is_none_when_nothing_is_available():
    result = signals.readiness(rhr={}, sleep={}, day={}, load={}, weights=WEIGHTS)
    assert result["score"] is None
    assert result["band"] == "unknown"
    assert result["inputs_used"] == []


def test_readiness_stays_in_range_at_extremes():
    for delta, hours, bb, stress, ratio in [(-20, 12, 100, 0, 1.0), (30, 0.5, 0, 100, 4.0)]:
        result = signals.readiness(
            rhr={"delta": delta}, sleep={"last_night_hours": hours, "target_hours": 7.5},
            day={"bb_wake": bb, "stress_avg": stress}, load={"ratio": ratio}, weights=WEIGHTS)
        assert 0 <= result["score"] <= 100


def test_load_score_does_not_punish_a_taper_like_a_spike():
    """An easy week is a plan, not a red flag; a spike is a red flag."""
    assert signals._score_load(0.6) > signals._score_load(1.9)


# --------------------------------------------------------------------------
# projection
# --------------------------------------------------------------------------

def test_riegel_matches_the_published_formula():
    runs = [{"miles": 13.1094, "duration_seconds": 6000, "date": "2026-09-01",
             "name": "half", "pace": "7:38", "avg_hr": 165}]
    result = signals.riegel_prediction(runs)
    expected = 6000 * (signals.MARATHON_MILES / 13.1094) ** 1.06
    assert result["predicted_seconds"] == pytest.approx(round(expected), abs=1)


def test_riegel_ignores_short_runs():
    runs = [{"miles": 3.0, "duration_seconds": 1500, "date": "2026-09-01",
             "name": "short", "pace": "8:20", "avg_hr": 160}]
    assert signals.riegel_prediction(runs)["predicted_seconds"] is None


def test_riegel_headline_is_the_fastest_projection():
    runs = [
        {"miles": 6.0, "duration_seconds": 3600, "date": "2026-09-01", "name": "easy",
         "pace": "10:00", "avg_hr": 140},
        {"miles": 6.0, "duration_seconds": 3000, "date": "2026-09-08", "name": "fast",
         "pace": "8:20", "avg_hr": 170},
    ]
    result = signals.riegel_prediction(runs)
    assert result["basis"]["name"] == "fast"
    assert result["conservative"]["basis"]["name"] == "easy"
    assert result["predicted_seconds"] < result["conservative"]["seconds"]


def test_riegel_reports_the_longest_run_separately():
    runs = [
        {"miles": 6.0, "duration_seconds": 2900, "date": "2026-09-08", "name": "fast short",
         "pace": "8:03", "avg_hr": 172},
        {"miles": 13.1, "duration_seconds": 7080, "date": "2026-09-12", "name": "long",
         "pace": "9:00", "avg_hr": 168},
    ]
    result = signals.riegel_prediction(runs)
    assert result["from_longest"]["basis"]["name"] == "long"


# --------------------------------------------------------------------------
# end-to-end
# --------------------------------------------------------------------------

def test_build_produces_a_complete_payload_from_synthetic_data():
    as_of = date(2026, 9, 28)
    days, activities = {}, []
    for i in range(40):
        day = as_of - timedelta(days=i)
        days[day.isoformat()] = make_day(day.isoformat(), rhr=50, sleep_hours=7.6,
                                         stress=25, bb_wake=80)
        if day.weekday() in (1, 3, 6):
            activities.append(make_activity(day.isoformat(), 55, avg_hr=150,
                                            miles=6.0, activity_id=1000 + i))
    plan = {
        "race": {"name": "Marathon", "date": "2026-11-15", "goal": "Sub-3:45:00",
                 "goal_seconds": 13500},
        "days": {as_of.isoformat(): {"session": "Long run 12mi easy", "target": "9:30/mi"}},
        "weeks": [{"week": 1, "phase": "Week 1", "headline": "h", "miles_target": 30,
                   "start": "2026-09-21", "end": "2026-09-27"}],
        "key_session_keywords": ["long run"],
        "paces": {"marathon": "8:35"},
        "fuel": {"daily_baseline": {"protein": "1.6–2.0g/kg/day"}},
    }
    result = signals.build(as_of=as_of, days=days, activities=activities,
                           plan=plan, config={"weight_kg": 70, "sleep_target_hours": 7.5})

    assert result["readiness"]["score"] is not None
    assert result["load"]["ratio"] is not None
    assert result["load"]["zone"] == "optimal"
    assert result["race"]["days_to_race"] == 48
    assert result["projection"]["predicted_seconds"] is not None
    assert result["fuel"]["carbs_grams"] == 560  # long run day: 8g/kg x 70kg
    assert result["meta"]["schema_version"] == 2


def test_build_survives_a_completely_empty_store():
    """A first run, or a totally failed sync, must render rather than crash."""
    result = signals.build(as_of=date(2026, 9, 28), days={}, activities=[],
                           plan={"race": {}, "days": {}}, config={})
    assert result["readiness"]["score"] is None
    assert result["load"]["ratio"] is None
    assert result["fitness"]["miles_28day"] == 0
    assert result["fuel"]["carbs_grams"] is None


def test_build_reports_unavailable_device_signals():
    """HRV absence has to surface as 'unavailable', never as a silent zero."""
    as_of = date(2026, 9, 28)
    days = {(as_of - timedelta(days=i)).isoformat():
            make_day((as_of - timedelta(days=i)).isoformat(), rhr=50, sleep_hours=7.5)
            for i in range(14)}
    result = signals.build(as_of=as_of, days=days, activities=[],
                           plan={"race": {}, "days": {}}, config={})
    flagged = {item["signal"] for item in result["meta"]["unavailable_signals"]}
    assert "HRV" in flagged
    assert "Garmin Training Readiness" in flagged
