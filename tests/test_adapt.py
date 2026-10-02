"""
Tests for plan matching and the adaptive layer.

The matching rules carry real risk of being quietly wrong in a way that erodes
trust: a dashboard that calls a completed run "missed", or scolds you at
breakfast for a session you're about to do, gets ignored within a week. The
adaptation rules carry the opposite risk -- silently rewriting the plan.
"""

import sys
from datetime import date, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import adapt  # noqa: E402


def plan_day(kind, miles, key=False, lift=None, session=None):
    return {"session": session or f"{kind} {miles}mi", "target": "x", "kind": kind,
            "miles": miles, "key": key, "lift": lift}


# A single Mon-Sun week: Mon rest, Tue quality 6, Wed easy 4, Thu medium-long 7,
# Fri rest, Sat easy 3, Sun long 11.
WEEK_START = date(2026, 9, 21)


def week_plan():
    kinds = [("rest", 0, False), ("quality", 6, True), ("easy", 4, False),
             ("medium_long", 7, True), ("rest", 0, False), ("easy", 3, False),
             ("long", 11, True)]
    return {
        (WEEK_START + timedelta(days=i)).isoformat(): plan_day(k, m, key)
        for i, (k, m, key) in enumerate(kinds)
    }


def run(day_offset, miles):
    return {"date": (WEEK_START + timedelta(days=day_offset)).isoformat(), "miles": miles}


def iso(day_offset):
    return (WEEK_START + timedelta(days=day_offset)).isoformat()


# --------------------------------------------------------------------------
# matching
# --------------------------------------------------------------------------

def test_full_distance_on_the_planned_day_is_done():
    statuses = adapt.match_plan(week_plan(), [run(1, 6.2)], WEEK_START + timedelta(days=6))
    assert statuses[iso(1)]["status"] == "done"
    assert statuses[iso(1)]["pct"] == 103


def test_sixty_percent_counts_as_done():
    """The threshold exists so a slightly short run isn't punished as a failure."""
    statuses = adapt.match_plan(week_plan(), [run(1, 3.6)], WEEK_START + timedelta(days=6))
    assert statuses[iso(1)]["status"] == "done"   # 3.6 / 6 = 60%


def test_below_sixty_percent_is_cut_short_not_missed():
    statuses = adapt.match_plan(week_plan(), [run(1, 2.0)], WEEK_START + timedelta(days=6))
    assert statuses[iso(1)]["status"] == "short"
    assert statuses[iso(1)]["pct"] == 33


def test_nothing_recorded_is_missed():
    statuses = adapt.match_plan(week_plan(), [], WEEK_START + timedelta(days=6))
    assert statuses[iso(1)]["status"] == "missed"
    assert statuses[iso(3)]["status"] == "missed"
    # Sunday is as_of here, so it is still in progress rather than missed.
    assert statuses[iso(6)]["status"] == "today"


def test_a_run_on_the_wrong_day_fills_the_nearest_miss():
    """Shifting a session by a day is normal life, not a failure."""
    # Ran on Wednesday (planned 4) and also on Monday (planned rest). Monday's run
    # should fill Tuesday's missed quality rather than reading as a bonus + a miss.
    statuses = adapt.match_plan(week_plan(), [run(0, 6.0)], WEEK_START + timedelta(days=6))
    assert statuses[iso(1)]["status"] == "moved"
    assert statuses[iso(1)]["moved_from"] == iso(0)


def test_a_shifted_run_is_counted_once_not_twice():
    """A run moved to fill another day must not also count where it was run.

    Left unfixed this inflates the week's mileage by the distance of every
    shifted run -- a 5-mile Monday run filling a missed Thursday reported the
    week 5 miles heavier than it was.
    """
    plan = week_plan()
    statuses = adapt.match_plan(plan, [run(0, 5.0)], WEEK_START + timedelta(days=6))
    assert statuses[iso(1)]["status"] == "moved"
    assert statuses[iso(1)]["actual_miles"] == 5.0
    # The source day keeps a record of the run but contributes no mileage.
    assert statuses[iso(0)]["actual_miles"] == 0.0
    assert statuses[iso(0)]["moved_to"] == iso(1)
    assert statuses[iso(0)]["moved_miles"] == 5.0
    summary = adapt.week_summary(plan, statuses, WEEK_START.isoformat())
    assert summary["actual_miles"] == 5.0        # not 10.0


def test_week_mileage_equals_the_sum_of_the_runs():
    """Whatever the matching decides, the week total is what was actually run."""
    plan = week_plan()
    runs = [run(0, 5.0), run(1, 6.1), run(6, 9.1)]
    statuses = adapt.match_plan(plan, runs, WEEK_START + timedelta(days=6))
    summary = adapt.week_summary(plan, statuses, WEEK_START.isoformat())
    assert summary["actual_miles"] == pytest.approx(20.2, abs=0.05)


def test_a_run_that_satisfied_its_own_day_is_not_reused():
    statuses = adapt.match_plan(week_plan(), [run(1, 6.0)], WEEK_START + timedelta(days=6))
    assert statuses[iso(1)]["status"] == "done"
    assert statuses[iso(3)]["status"] == "missed"   # Thursday not filled by Tuesday's run


def test_a_session_planned_for_today_is_neither_done_nor_missed():
    """The most important one: don't scold at breakfast for a run not yet done."""
    statuses = adapt.match_plan(week_plan(), [], WEEK_START + timedelta(days=1))
    assert statuses[iso(1)]["status"] == "today"
    summary = adapt.week_summary(week_plan(), statuses, WEEK_START.isoformat())
    assert summary["key_missed"] == 0
    assert summary["key_pending"] == 1


def test_todays_session_flips_to_done_once_recorded():
    statuses = adapt.match_plan(week_plan(), [run(1, 6.1)], WEEK_START + timedelta(days=1))
    assert statuses[iso(1)]["status"] == "done"


def test_future_days_are_upcoming():
    statuses = adapt.match_plan(week_plan(), [], WEEK_START)
    assert statuses[iso(6)]["status"] == "upcoming"


def test_a_run_on_a_rest_day_is_not_a_miss():
    statuses = adapt.match_plan({iso(0): plan_day("rest", 0)}, [run(0, 4.0)], WEEK_START)
    assert statuses[iso(0)]["status"] == "rest"
    assert statuses[iso(0)]["actual_miles"] == 4.0


def test_week_summary_totals_planned_and_actual():
    statuses = adapt.match_plan(week_plan(), [run(1, 6.0), run(6, 11.0)],
                               WEEK_START + timedelta(days=6))
    summary = adapt.week_summary(week_plan(), statuses, WEEK_START.isoformat())
    assert summary["planned_miles"] == 31
    assert summary["actual_miles"] == 17.0
    assert summary["key_planned"] == 3
    assert summary["key_done"] == 2
    assert summary["key_missed"] == 1


# --------------------------------------------------------------------------
# rescheduling
# --------------------------------------------------------------------------

def test_reschedule_never_lands_the_day_before_a_key_session():
    """Moving a hard run onto the eve of the long run just moves the problem."""
    plan = week_plan()
    statuses = adapt.match_plan(plan, [], WEEK_START + timedelta(days=6))
    slot = adapt.find_reschedule_slot(plan, statuses, iso(3))
    # Fri (4) is rest but Sat (5) is not key, so Fri is legal; Sat is illegal
    # because Sun is the long run.
    assert slot == iso(4)


def test_reschedule_returns_none_when_the_week_is_out_of_room():
    plan = week_plan()
    statuses = adapt.match_plan(plan, [], WEEK_START + timedelta(days=6))
    assert adapt.find_reschedule_slot(plan, statuses, iso(5)) is None


# --------------------------------------------------------------------------
# adaptation
# --------------------------------------------------------------------------

def make_data(readiness, as_of_offset=1, history_scores=None, fitness=50, fatigue=50):
    plan = week_plan()
    as_of = WEEK_START + timedelta(days=as_of_offset)
    history = [{"date": (as_of - timedelta(days=i)).isoformat(),
                "readiness_score": (history_scores or [70] * 4)[min(i, 3)]}
               for i in range(4)][::-1]
    return {
        "meta": {"as_of": as_of.isoformat()},
        "plan_days": plan,
        "paces": {"easy": "9:50–10:20", "long": "9:20–9:50"},
        "readiness": {"score": readiness},
        "recovery": {"history": history},
        "form": {"fitness": fitness, "fatigue": fatigue},
    }


def test_green_readiness_changes_nothing():
    data = make_data(82)
    statuses = adapt.match_plan(data["plan_days"], [], WEEK_START + timedelta(days=1))
    result = adapt.adapt(data, statuses)
    assert result["level"] == "green"
    assert result["suggestions"]["today"]["changed"] is False


def test_moderate_readiness_makes_quality_easy_at_the_same_distance():
    data = make_data(55)
    statuses = adapt.match_plan(data["plan_days"], [], WEEK_START + timedelta(days=1))
    result = adapt.adapt(data, statuses)
    suggestion = result["suggestions"]["today"]
    assert suggestion["changed"] is True
    assert suggestion["suggested"]["kind"] == "easy"
    # The volume is the point -- distance is preserved.
    assert suggestion["suggested"]["miles"] == suggestion["original"]["miles"]


def test_moderate_readiness_trims_the_long_run_by_ten_percent():
    data = make_data(55, as_of_offset=6)
    statuses = adapt.match_plan(data["plan_days"], [], WEEK_START + timedelta(days=6))
    result = adapt.adapt(data, statuses)
    suggestion = result["suggestions"]["today"]
    assert suggestion["suggested"]["kind"] == "long"
    assert suggestion["suggested"]["miles"] == pytest.approx(9.9)


def test_low_readiness_rests_and_reschedules_the_key_session():
    data = make_data(30)
    statuses = adapt.match_plan(data["plan_days"], [], WEEK_START + timedelta(days=1))
    result = adapt.adapt(data, statuses)
    suggestion = result["suggestions"]["today"]
    assert suggestion["suggested"]["kind"] == "rest"
    assert suggestion["suggested"]["miles"] == 0
    assert suggestion["suggested"]["move_to"] is not None


def test_lower_b_is_the_first_lift_dropped():
    """The plan states the sacrifice order: drop Lower B, then trim Lower A."""
    plan = week_plan()
    plan[iso(1)]["lift"] = "lower_b"
    data = make_data(30)
    data["plan_days"] = plan
    statuses = adapt.match_plan(plan, [], WEEK_START + timedelta(days=1))
    result = adapt.adapt(data, statuses)
    suggested = result["suggestions"]["today"]["suggested"]
    assert suggested["lift"] is None
    assert "Lower B" in suggested["lift_note"]


def test_lower_a_is_trimmed_rather_than_dropped():
    plan = week_plan()
    plan[iso(1)]["lift"] = "lower_a"
    data = make_data(30)
    data["plan_days"] = plan
    statuses = adapt.match_plan(plan, [], WEEK_START + timedelta(days=1))
    suggested = adapt.adapt(data, statuses)["suggestions"]["today"]["suggested"]
    assert suggested["lift"] == "lower_a"
    assert "2 sets" in suggested["lift_note"]


def test_upper_body_survives_a_bad_day():
    """Upper body is last to be cut -- the opposite of a generic scale-everything
    rule, and what the plan actually says to do."""
    plan = week_plan()
    plan[iso(1)]["lift"] = "upper_push"
    data = make_data(30)
    data["plan_days"] = plan
    statuses = adapt.match_plan(plan, [], WEEK_START + timedelta(days=1))
    suggested = adapt.adapt(data, statuses)["suggestions"]["today"]["suggested"]
    assert suggested["lift"] == "upper_push"


def test_absorb_week_fires_on_three_of_four_days_below_55():
    data = make_data(60, history_scores=[50, 48, 52, 70])
    statuses = adapt.match_plan(data["plan_days"], [], WEEK_START + timedelta(days=1))
    result = adapt.adapt(data, statuses)
    assert result["absorb_week"] is True
    assert any("absorb" in t["effect"].lower() for t in result["triggers"])


def test_absorb_week_does_not_fire_on_two_bad_days():
    data = make_data(60, history_scores=[50, 48, 70, 72])
    statuses = adapt.match_plan(data["plan_days"], [], WEEK_START + timedelta(days=1))
    assert adapt.adapt(data, statuses)["absorb_week"] is False


def test_high_fatigue_ratio_eases_the_next_quality_session():
    data = make_data(80, fitness=40, fatigue=60)      # ratio 1.5
    statuses = adapt.match_plan(data["plan_days"], [], WEEK_START + timedelta(days=1))
    result = adapt.adapt(data, statuses)
    assert result["fatigue_fitness_ratio"] == pytest.approx(1.5)
    assert result["suggestions"]["today"]["suggested"]["kind"] == "easy"


def test_a_healthy_fatigue_ratio_leaves_quality_alone():
    data = make_data(80, fitness=50, fatigue=55)      # ratio 1.1
    statuses = adapt.match_plan(data["plan_days"], [], WEEK_START + timedelta(days=1))
    assert adapt.adapt(data, statuses)["suggestions"]["today"]["changed"] is False


def test_adaptation_never_mutates_the_base_plan():
    """The whole design rests on this: the base plan is immutable."""
    data = make_data(30)
    before = {k: dict(v) for k, v in data["plan_days"].items()}
    statuses = adapt.match_plan(data["plan_days"], [], WEEK_START + timedelta(days=1))
    adapt.adapt(data, statuses)
    assert data["plan_days"] == before


def test_the_original_session_is_always_kept_beside_the_suggestion():
    data = make_data(30)
    statuses = adapt.match_plan(data["plan_days"], [], WEEK_START + timedelta(days=1))
    result = adapt.adapt(data, statuses)
    suggestion = result["suggestions"]["today"]
    assert suggestion["original"]["session"]
    assert suggestion["suggested"]["session"] != suggestion["original"]["session"]


# --------------------------------------------------------------------------
# lift matching
# --------------------------------------------------------------------------
# Garmin has no lifting activity type on this watch, so a gym session is filed as
# "other". A 30-90 minute "other" activity on a planned lift day is taken to be
# that lift. The risk is obvious: it is an inference, and it must never be
# presented as a recording.

def lift_plan():
    """Mon upper push, Tue lower A, Thu upper pull, Sat lower B."""
    plan = week_plan()
    for offset, lift in ((0, "upper_push"), (1, "lower_a"), (3, "upper_pull"), (5, "lower_b")):
        plan[iso(offset)]["lift"] = lift
    return plan


def other(day_offset, minutes, hr=102):
    return {"activityId": 9000 + day_offset,
            "startTimeLocal": f"{iso(day_offset)} 18:00:00",
            "activityType": {"typeKey": "other"},
            "duration": minutes * 60, "averageHR": hr}


def typed(day_offset, type_key, minutes):
    return {"activityId": 9500 + day_offset,
            "startTimeLocal": f"{iso(day_offset)} 18:00:00",
            "activityType": {"typeKey": type_key},
            "duration": minutes * 60}


def test_an_other_activity_in_the_band_completes_the_lift():
    lifts = adapt.match_lifts(lift_plan(), [other(0, 49)], WEEK_START + timedelta(days=6))
    assert lifts[iso(0)]["status"] == "done"
    assert lifts[iso(0)]["minutes"] == 49


def test_an_inferred_completion_is_flagged_as_inferred():
    """It must never read as a recorded strength session."""
    lifts = adapt.match_lifts(lift_plan(), [other(0, 49)], WEEK_START + timedelta(days=6))
    assert lifts[iso(0)]["inferred"] is True
    assert "assumed" in lifts[iso(0)]["basis"]


def test_a_real_strength_activity_is_not_flagged_as_inferred():
    lifts = adapt.match_lifts(lift_plan(),
                              [typed(0, "strength_training", 45)],
                              WEEK_START + timedelta(days=6))
    assert lifts[iso(0)]["status"] == "done"
    assert lifts[iso(0)]["inferred"] is False


@pytest.mark.parametrize("minutes,expected", [
    (29, "missed"),    # too short — a warm-up or a walk
    (30, "done"),      # boundary
    (90, "done"),      # boundary
    (91, "missed"),    # too long to be a lift
])
def test_the_duration_band_is_respected(minutes, expected):
    lifts = adapt.match_lifts(lift_plan(), [other(0, minutes)],
                              WEEK_START + timedelta(days=6))
    assert lifts[iso(0)]["status"] == expected


def test_a_run_on_a_lift_day_does_not_complete_the_lift():
    """Most lift days also carry a run. They must not satisfy each other."""
    lifts = adapt.match_lifts(lift_plan(), [typed(0, "running", 45)],
                              WEEK_START + timedelta(days=6))
    assert lifts[iso(0)]["status"] == "missed"


def test_a_lift_and_a_run_are_tracked_independently():
    plan = lift_plan()
    as_of = WEEK_START + timedelta(days=6)
    # Monday is a planned rest day for running but carries a lift. Ran Tuesday.
    runs = [run(1, 6.0)]
    statuses = adapt.match_plan(plan, runs, as_of)
    lifts = adapt.match_lifts(plan, [other(0, 50)], as_of)
    assert statuses[iso(1)]["status"] == "done"      # the run
    assert lifts[iso(0)]["status"] == "done"         # the lift, a different day
    assert lifts[iso(1)]["status"] == "missed"       # Tuesday's lift wasn't done


def test_a_lift_planned_for_today_is_not_yet_missed():
    lifts = adapt.match_lifts(lift_plan(), [], WEEK_START)
    assert lifts[iso(0)]["status"] == "today"


def test_future_lifts_are_upcoming():
    lifts = adapt.match_lifts(lift_plan(), [], WEEK_START)
    assert lifts[iso(3)]["status"] == "upcoming"


def test_days_with_no_planned_lift_are_absent_entirely():
    lifts = adapt.match_lifts(lift_plan(), [other(2, 50)], WEEK_START + timedelta(days=6))
    assert iso(2) not in lifts          # Wednesday has no lift in this plan


def test_the_week_summary_counts_lifts():
    plan = lift_plan()
    as_of = WEEK_START + timedelta(days=6)
    lifts = adapt.match_lifts(plan, [other(0, 50), other(3, 62)], as_of)
    summary = adapt.week_summary(plan, adapt.match_plan(plan, [], as_of),
                                 WEEK_START.isoformat(), lifts)
    assert summary["lifts_planned"] == 4
    assert summary["lifts_done"] == 2
    assert summary["lifts_missed"] == 2
