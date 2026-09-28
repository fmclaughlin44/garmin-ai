"""
Tests for filling nights the watch missed.

The risk here is subtle: an estimate that silently passes for a measurement.
Sleep and the nocturnal drop carry 45% of the readiness weight between them, so
a hand-typed number quietly replacing a measured one — or a stage breakdown
invented from a typical ratio — would corrupt the score while looking fine.
"""

import sys
from datetime import date, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import signals  # noqa: E402


def garmin_day(hours=None, rhr=50):
    payload = {"stats": {"restingHeartRate": rhr}}
    if hours is not None:
        seconds = int(hours * 3600)
        payload["sleep"] = {"dailySleepDTO": {
            "sleepTimeSeconds": seconds,
            "deepSleepSeconds": int(seconds * 0.2),
            "remSleepSeconds": int(seconds * 0.2),
        }}
    return payload


# --------------------------------------------------------------------------
# merging manual entries
# --------------------------------------------------------------------------

def test_a_manual_entry_fills_a_missing_night():
    m = signals.extract_day_metrics("2026-09-20", garmin_day(None), {"sleep_hours": 7.25})
    assert m["sleep_hours"] == 7.25
    assert m["sleep_source"] == "manual"


def test_a_measurement_always_beats_a_manual_entry():
    """Garmin backfills a night's sleep a day or two late. When it does, the
    measurement must replace the estimate, not the other way round."""
    m = signals.extract_day_metrics("2026-09-20", garmin_day(7.0), {"sleep_hours": 9.0})
    assert m["sleep_hours"] == 7.0
    assert m["sleep_source"] == "garmin"


def test_stages_are_not_invented_for_a_hand_entered_night():
    """A typical deep/REM ratio would look plausible and mean nothing."""
    m = signals.extract_day_metrics("2026-09-20", garmin_day(None), {"sleep_hours": 7.5})
    assert m["deep_pct"] is None
    assert m["rem_pct"] is None


def test_an_imported_night_is_labelled_by_its_source():
    m = signals.extract_day_metrics("2026-09-20", garmin_day(None),
                                    {"sleep_hours": 7.5, "source": "apple_health"})
    assert m["sleep_source"] == "apple_health"


def test_a_night_with_neither_stays_empty():
    m = signals.extract_day_metrics("2026-09-20", garmin_day(None), None)
    assert m["sleep_hours"] is None
    assert m["sleep_source"] is None


def test_a_filled_night_restores_the_sleep_component_of_readiness():
    """The point of filling a gap: readiness goes back to being built from all
    its inputs rather than silently fewer."""
    weights = {"resting_hr": 25, "nocturnal_drop": 20, "sleep": 25,
               "body_battery": 15, "stress": 5, "training_load": 10}
    empty = signals.readiness({"delta": 0}, signals.sleep_summary(
        [signals.extract_day_metrics("2026-09-20", garmin_day(None), None)],
        "2026-09-20", 7.5), {}, {}, weights)
    filled = signals.readiness({"delta": 0}, signals.sleep_summary(
        [signals.extract_day_metrics("2026-09-20", garmin_day(None), {"sleep_hours": 8.0})],
        "2026-09-20", 7.5), {}, {}, weights)
    assert "sleep" not in empty["inputs_used"]
    assert "sleep" in filled["inputs_used"]


# --------------------------------------------------------------------------
# gap reporting
# --------------------------------------------------------------------------

def history_with(dates_and_hours):
    return [signals.extract_day_metrics(d, garmin_day(h), None) for d, h in dates_and_hours]


def test_gaps_lists_only_nights_without_sleep():
    as_of = date(2026, 9, 20)
    rows = [((as_of - timedelta(days=i)).isoformat(), None if i in (1, 3) else 7.5)
            for i in range(5)][::-1]
    gaps = signals.sleep_gaps(history_with(rows), as_of)
    assert gaps["missing_count"] == 2
    assert (as_of - timedelta(days=1)).isoformat() in gaps["missing"]


def test_gaps_flags_last_night_specifically():
    as_of = date(2026, 9, 20)
    rows = [((as_of - timedelta(days=i)).isoformat(), None if i == 0 else 7.5)
            for i in range(4)][::-1]
    assert signals.sleep_gaps(history_with(rows), as_of)["last_night_missing"] is True


def test_gaps_ignores_nights_outside_the_window():
    as_of = date(2026, 9, 20)
    rows = [("2026-01-01", None), (as_of.isoformat(), 7.5)]
    assert signals.sleep_gaps(history_with(rows), as_of, window_days=30)["missing_count"] == 0
