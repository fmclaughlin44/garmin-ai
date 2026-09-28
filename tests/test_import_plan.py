"""
Tests for the plan importer.

marathon_plan.json is written the way a coach writes a plan -- prose titles,
mileage ranges, durations -- and the pipeline needs numbers. Every number the
dashboard shows about the plan comes out of this parser, so a quiet misread here
propagates into weekly targets, adherence, nutrition bands and the adaptive
layer. The first version read "Easy 30-40min" as a 35-mile run and reported
week 1 at 96 miles.
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "plan"))

import import_plan  # noqa: E402


# --------------------------------------------------------------------------
# distance parsing
# --------------------------------------------------------------------------

def test_a_plain_distance():
    assert import_plan.parse_miles("Long Run 16mi + Lower B") == 16


def test_a_range_becomes_its_midpoint():
    assert import_plan.parse_miles("Upper A (Push) + Easy Run 4-5mi") == 4.5


def test_minutes_are_not_read_as_miles():
    """The regression: 'mi' must not match the start of 'min'.

    Unguarded, "Easy 30-40min" parsed as a 35-mile run and put week 1 at 96 miles.
    """
    miles = import_plan.parse_miles("Easy 30-40min + strides")
    assert 3 < miles < 5          # 35 minutes at easy pace, not 35 miles


def test_a_single_duration_is_converted_at_easy_pace():
    assert import_plan.parse_miles("15-20min shakeout + strides") == pytest.approx(1.8, abs=0.2)


def test_interval_work_adds_a_warm_up_and_cool_down():
    # 4 x 1.5mi = 6 miles of work, plus ~3 miles either side.
    assert import_plan.parse_miles("MP 4x1.5mi @ 8:34") == 9


def test_an_explicit_warm_up_spec_is_used_verbatim():
    # "2WU/3/1CD" means exactly 6 miles; don't add a second allowance on top.
    assert import_plan.parse_miles("Tempo 3mi @ 8:10 2WU/3/1CD") == 6


def test_race_day_is_the_full_distance():
    assert import_plan.parse_miles("MARATHON — RACE DAY") == pytest.approx(26.2188)


def test_a_cross_training_duration_is_not_mileage():
    assert import_plan.parse_miles("Easy cross-train 30-40min") == 0


# --------------------------------------------------------------------------
# classification
# --------------------------------------------------------------------------

@pytest.mark.parametrize("title,disc,expected", [
    ("Long Run 20mi + Lower B", "run", "long"),
    ("LONG RUN 20mi + Lower B", "run", "long"),
    ("Tempo 4mi @ 8:10", "run", "quality"),
    ("MP 2x3mi @ 8:34", "run", "quality"),
    ("Cruise 3x2mi @ 8:15", "run", "quality"),
    ("MP tune-up 4mi @ 8:34", "run", "quality"),
    ("Short sharp: 2mi @ MP + strides", "run", "quality"),
    ("MARATHON — RACE DAY", "run", "race"),
    ("Upper A (Push) + Easy Run 4-5mi", "lift", "easy"),
    ("Easy shakeout 3mi", "run", "easy"),
    ("Easy cross-train 30-40min", "swim", "cross"),
    ("Optional easy 3-4mi / cross-train", "rest", "rest"),
    ("Rest or 20min easy jog", "rest", "rest"),
    ("Full rest / 10-15min shakeout", "rest", "rest"),
])
def test_classification(title, disc, expected):
    assert import_plan.classify({"title": title, "disc": disc}) == expected


@pytest.mark.parametrize("title,expected", [
    ("Upper A (Push) + Easy Run 4-5mi", "upper_push"),
    ("Upper B (Pull) + Easy Run 4-5mi", "upper_pull"),
    ("Easy Run 5-6mi + Lower A", "lower_a"),
    ("Long Run 12mi + Lower B", "lower_b"),
    ("Light Upper (full body) + Easy 4mi", "upper_full"),
    ("Easy shakeout 3mi", None),
])
def test_lift_detection(title, expected):
    assert import_plan.find_lift({"title": title}) == expected


# --------------------------------------------------------------------------
# the whole plan
# --------------------------------------------------------------------------

def test_the_real_plan_imports_with_sane_volumes():
    plan = import_plan.build()
    assert len(plan["days"]) == 56
    assert plan["block"]["weeks"] == 8

    volumes = [w["miles_target"] for w in plan["weeks"]]
    # A marathon build for this athlete lives in the 20s to mid-40s. Anything
    # outside that means the parser has misread something.
    assert all(18 <= v <= 55 for v in volumes), volumes
    # Peak week carries more than the recovery week it started from.
    assert max(volumes) > volumes[0]


def test_the_long_run_is_on_saturday_every_week():
    """The plan puts the long run on Saturday; an off-by-one here would silently
    misalign adherence for the whole block."""
    plan = import_plan.build()
    from datetime import datetime
    for iso, entry in plan["days"].items():
        if entry["kind"] == "long":
            assert datetime.fromisoformat(iso).weekday() == 5, iso


def test_quality_sessions_land_on_wednesday_outside_race_week():
    plan = import_plan.build()
    from datetime import datetime
    weekdays = {datetime.fromisoformat(iso).weekday()
                for iso, e in plan["days"].items()
                if e["kind"] == "quality" and e["week"] < 8}
    assert weekdays == {2}


def test_long_runs_and_quality_are_the_key_sessions():
    plan = import_plan.build()
    for entry in plan["days"].values():
        if entry["kind"] in ("long", "quality", "race"):
            assert entry["key"] is True
        if entry["kind"] in ("rest", "cross"):
            assert entry["key"] is False


def test_rest_and_cross_days_carry_no_mileage_target():
    plan = import_plan.build()
    for entry in plan["days"].values():
        if entry["kind"] in ("rest", "cross"):
            assert entry["miles"] == 0


def test_goal_and_paces_come_from_the_plan_file():
    plan = import_plan.build()
    assert plan["race"]["goal_seconds"] == 13500        # 3:45:00
    assert plan["paces"]["marathon"] == "8:34"
    assert plan["paces"]["cruise"] == "8:15"
    assert plan["athlete"]["maxHR"] == 194
    assert len(plan["rules"]) >= 5
    assert "upperA_push" in plan["lifting"]
