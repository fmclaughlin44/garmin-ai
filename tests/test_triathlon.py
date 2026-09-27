"""
Tests for the reference-race split detection and the full-distance extrapolation.

The split detection has a specific trap: on the real race the swim leg came back
typed as `indoor_cardio`, not as a swim. Guessing silently would be worse than
not guessing, so the inference has to be flagged — and these tests pin that down
along with the arithmetic.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import signals  # noqa: E402

RACE = "2026-09-12"


def act(type_key, minutes, miles, hr=150, name="x", day=RACE, activity_id=1):
    return {
        "activityId": activity_id,
        "startTimeLocal": f"{day} 07:00:00",
        "activityType": {"typeKey": type_key},
        "activityName": name,
        "duration": minutes * 60,
        "distance": miles * signals.METERS_PER_MILE,
        "averageHR": hr,
    }


IM_CONFIG = {"swim_miles": 2.4, "bike_miles": 112.0, "run_miles": signals.MARATHON_MILES,
             "transition_minutes": 15}


# --------------------------------------------------------------------------
# split detection
# --------------------------------------------------------------------------

def test_three_clean_legs_are_detected():
    activities = [act("lap_swimming", 39, 1.2, activity_id=1),
                  act("cycling", 190, 56.7, activity_id=2),
                  act("running", 118, 13.1, activity_id=3)]
    ref = signals.reference_race_splits(activities, RACE)
    assert ref["complete"] is True
    assert ref["legs"]["bike"]["minutes"] == 190
    assert ref["moving_minutes"] == 347
    assert ref["moving_time"] == "5:47:00"
    assert ref["note"] is None


def test_an_unclassified_session_stands_in_for_the_swim_but_is_flagged():
    """The real race recorded the swim as indoor_cardio. Use it, but say so."""
    activities = [act("indoor_cardio", 39, 0.0, activity_id=1),
                  act("cycling", 190, 56.7, activity_id=2),
                  act("running", 118, 13.1, activity_id=3)]
    ref = signals.reference_race_splits(activities, RACE)
    assert ref["complete"] is True
    assert ref["legs"]["swim"]["inferred"] is True
    assert "inferred" in ref["note"]


def test_a_real_swim_activity_beats_an_unclassified_one():
    activities = [act("indoor_cardio", 20, 0.0, activity_id=1),
                  act("lap_swimming", 39, 1.2, activity_id=2),
                  act("cycling", 190, 56.7, activity_id=3),
                  act("running", 118, 13.1, activity_id=4)]
    ref = signals.reference_race_splits(activities, RACE)
    assert ref["legs"]["swim"]["minutes"] == 39
    assert ref["legs"]["swim"]["inferred"] is False


def test_activities_on_other_days_are_ignored():
    activities = [act("cycling", 190, 56.7, day="2026-09-11", activity_id=1),
                  act("running", 118, 13.1, activity_id=2)]
    ref = signals.reference_race_splits(activities, RACE)
    assert ref["legs"]["bike"] is None
    assert ref["complete"] is False


def test_an_incomplete_race_does_not_produce_a_projection():
    ref = signals.reference_race_splits([act("running", 118, 13.1)], RACE)
    projection = signals.ironman_projection(ref, IM_CONFIG)
    assert projection["available"] is False


# --------------------------------------------------------------------------
# extrapolation
# --------------------------------------------------------------------------

def full_reference():
    return signals.reference_race_splits(
        [act("lap_swimming", 39, 1.2, activity_id=1),
         act("cycling", 190, 56.7, activity_id=2),
         act("running", 118, 13.1, activity_id=3)], RACE)


def test_projection_is_a_range_not_a_single_number():
    p = signals.ironman_projection(full_reference(), IM_CONFIG)
    assert p["available"] is True
    fast = sum(r["fast_minutes"] for r in p["legs"]) + p["transition_minutes"]
    slow = sum(r["slow_minutes"] for r in p["legs"]) + p["transition_minutes"]
    assert fast < slow
    assert p["midpoint_seconds"] == pytest.approx((fast + slow) / 2 * 60, abs=60)


def test_each_leg_is_slowed_relative_to_the_half():
    """A full distance must never project faster per mile than the half it came from."""
    p = signals.ironman_projection(full_reference(), IM_CONFIG)
    for row in p["legs"]:
        fast_pace = row["fast_minutes"] / row["target_miles"]
        assert fast_pace >= row["reference_pace_min_per_mile"]


def test_transitions_are_included_in_the_total():
    base = signals.ironman_projection(full_reference(), IM_CONFIG)
    more = signals.ironman_projection(full_reference(), {**IM_CONFIG, "transition_minutes": 45})
    assert more["midpoint_seconds"] - base["midpoint_seconds"] == pytest.approx(30 * 60, abs=60)


def test_an_unrecorded_swim_distance_falls_back_to_the_half_distance():
    """The swim often logs no distance; assume 1.2mi rather than dividing by zero."""
    activities = [act("indoor_cardio", 39, 0.0, activity_id=1),
                  act("cycling", 190, 56.7, activity_id=2),
                  act("running", 118, 13.1, activity_id=3)]
    p = signals.ironman_projection(signals.reference_race_splits(activities, RACE), IM_CONFIG)
    swim = next(r for r in p["legs"] if r["leg"] == "swim")
    assert swim["reference_miles"] == 1.2
    assert swim["fast_minutes"] > 39      # twice the distance, at least

def test_goal_comparison_only_when_a_goal_is_set():
    without = signals.ironman_projection(full_reference(), IM_CONFIG)
    assert without["goal_seconds"] is None
    assert without["on_track"] is False
    with_goal = signals.ironman_projection(full_reference(), {**IM_CONFIG, "goal_time": "15:00:00"})
    assert with_goal["goal_seconds"] == 54000
    assert with_goal["on_track"] is True


def test_insights_reference_the_athletes_own_numbers():
    ref = full_reference()
    p = signals.ironman_projection(ref, IM_CONFIG)
    from datetime import date
    lines = signals.triathlon_insights(ref, p, [], date(2026, 10, 1))
    assert lines
    assert any("mph" in line for line in lines)      # the bike speed he actually rode
