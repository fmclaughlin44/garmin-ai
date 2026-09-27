"""
Tests for the energy and nutrition layer.

The risk here is specific: a calorie target built on a bad maintenance figure is
worse than no target at all, because it looks authoritative. The two things that
must hold are that maintenance comes from complete days only, and that the macro
split actually periodises with training rather than just claiming to.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import signals  # noqa: E402

CONFIG = {
    "weight_lb": 180,
    "protein_per_lb": 1.0,
    "deficit_by_day": {"big": 0, "quality": -200, "easy": -400, "recovery": -450},
    "fat_g_per_lb_by_day": {"big": 0.35, "quality": 0.40, "easy": 0.50, "recovery": 0.55},
}


def day(bmr, active):
    return {"stats": {"bmrKilocalories": bmr, "activeKilocalories": active,
                      "totalKilocalories": bmr + active}}


def plan(kind, miles):
    return {"kind": kind, "miles": miles}


# --------------------------------------------------------------------------
# measured maintenance
# --------------------------------------------------------------------------

def test_maintenance_is_measured_not_estimated():
    days = {f"2026-09-{d:02d}": day(2200, 600) for d in range(10, 20)}
    energy = signals.energy_baseline([], days)
    assert energy["maintenance"] == 2800
    assert energy["bmr"] == 2200
    assert energy["days_used"] == 10


def test_todays_partial_day_is_excluded():
    """The important one: today's BMR is only accrued so far, so its total is
    always an undercount and would drag maintenance down every morning."""
    days = {f"2026-09-{d:02d}": day(2200, 600) for d in range(10, 20)}
    days["2026-09-20"] = day(900, 400)          # today, half-accrued
    energy = signals.energy_baseline([], days)
    assert energy["maintenance"] == 2800        # unchanged by the partial day
    assert energy["days_used"] == 10


def test_maintenance_is_none_without_calorie_data():
    energy = signals.energy_baseline([], {"2026-09-10": {"stats": {}}})
    assert energy["maintenance"] is None
    assert energy["days_used"] == 0


def test_maintenance_reports_its_observed_range():
    days = {"2026-09-10": day(2200, 300), "2026-09-11": day(2200, 900),
            "2026-09-12": day(2200, 600)}
    energy = signals.energy_baseline([], days)
    assert energy["range"] == [2500, 3100]


# --------------------------------------------------------------------------
# macro targets
# --------------------------------------------------------------------------

ENERGY = {"maintenance": 2800, "bmr": 2200, "avg_active": 600, "days_used": 10,
          "range": [2500, 3100], "note": ""}


def test_protein_is_one_gram_per_pound_regardless_of_day():
    for kind, miles in (("long", 18), ("easy", 4), ("rest", 0), ("quality", 8)):
        target = signals.nutrition(plan(kind, miles), ENERGY, CONFIG)
        assert target["protein_g"] == 180


def test_a_big_day_sits_at_maintenance_with_high_carb_and_low_fat():
    target = signals.nutrition(plan("long", 18), ENERGY, CONFIG)
    assert target["band"] == "big"
    assert target["target_kcal"] == 2800              # no deficit on a big day
    assert target["fat_g"] == 63                      # 0.35 g/lb
    assert target["split_pct"]["carbs"] >= 50
    assert target["split_pct"]["fat"] <= 22


def test_a_recovery_day_runs_a_deficit_with_lower_carb_and_higher_fat():
    target = signals.nutrition(plan("rest", 0), ENERGY, CONFIG)
    assert target["band"] == "recovery"
    assert target["target_kcal"] == 2350              # 2800 - 450
    assert target["fat_g"] == 99                      # 0.55 g/lb
    big = signals.nutrition(plan("long", 18), ENERGY, CONFIG)
    assert target["carbs_g"] < big["carbs_g"]
    assert target["fat_g"] > big["fat_g"]


def test_carbohydrate_rises_monotonically_with_the_day_s_demand():
    """The periodisation has to actually be there, not just be described."""
    carbs = [signals.nutrition(plan(k, m), ENERGY, CONFIG)["carbs_g"]
             for k, m in (("rest", 0), ("easy", 5), ("quality", 8), ("long", 18))]
    assert carbs == sorted(carbs)
    assert carbs[0] < carbs[-1]


def test_fat_falls_as_the_day_gets_harder():
    fats = [signals.nutrition(plan(k, m), ENERGY, CONFIG)["fat_g"]
            for k, m in (("rest", 0), ("easy", 5), ("quality", 8), ("long", 18))]
    assert fats == sorted(fats, reverse=True)


def test_macros_add_back_up_to_the_calorie_target():
    for kind, miles in (("long", 18), ("easy", 4), ("rest", 0), ("quality", 8)):
        target = signals.nutrition(plan(kind, miles), ENERGY, CONFIG)
        total = target["protein_g"] * 4 + target["carbs_g"] * 4 + target["fat_g"] * 9
        assert total == pytest.approx(target["target_kcal"], abs=4)


def test_a_long_run_gets_big_day_treatment_whatever_the_plan_calls_it():
    """A 14-mile 'medium-long' is a big day regardless of its label."""
    target = signals.nutrition(plan("medium_long", 14), ENERGY, CONFIG)
    assert target["band"] == "big"


def test_no_gram_targets_without_a_bodyweight():
    target = signals.nutrition(plan("long", 18), ENERGY, {"protein_per_lb": 1.0})
    assert target["carbs_g"] is None
    assert target["target_kcal"] is None
    assert "config.json" in target["note"]


def test_no_gram_targets_without_measured_maintenance():
    target = signals.nutrition(plan("long", 18), {"maintenance": None}, CONFIG)
    assert target["target_kcal"] is None
    assert target["protein_g"] == 180        # protein needs no calorie data


def test_carbs_never_go_negative_on_a_very_small_budget():
    tiny = {"maintenance": 900, "bmr": 900, "days_used": 3}
    target = signals.nutrition(plan("rest", 0), tiny, CONFIG)
    assert target["carbs_g"] >= 0
