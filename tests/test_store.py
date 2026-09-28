"""
Tests for the durability layer.

This module is the reason the project still has history: an earlier version
rebuilt one file on every sync and silently destroyed everything outside the
pull window. The rules that prevent a repeat are small and easy to break —
"a failed pull must not erase a good day", "a write must not leave a truncated
file" — and until now none of them were tested.

Every test runs against a temporary store, never the real one.
"""

import importlib
import json
import sys
from datetime import date
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import store as store_module  # noqa: E402


@pytest.fixture
def store(tmp_path, monkeypatch):
    """A store rooted in a temp directory, so tests never touch real data."""
    monkeypatch.setattr(store_module, "STORE", tmp_path / "store")
    monkeypatch.setattr(store_module, "DAYS_DIR", tmp_path / "store" / "days")
    monkeypatch.setattr(store_module, "ACTIVITIES_DIR", tmp_path / "store" / "activities")
    monkeypatch.setattr(store_module, "SNAPSHOTS_DIR", tmp_path / "store" / "snapshots")
    monkeypatch.setattr(store_module, "MANUAL_DIR", tmp_path / "store" / "manual")
    store_module.ensure_dirs()
    return store_module


def good_day(rhr=50):
    return {"stats": {"restingHeartRate": rhr}, "sleep": {"dailySleepDTO": {"sleepTimeSeconds": 27000}}}


# --------------------------------------------------------------------------
# days
# --------------------------------------------------------------------------

def test_a_day_round_trips(store):
    store.put_day("2026-09-20", good_day())
    assert store.get_day("2026-09-20")["stats"]["restingHeartRate"] == 50


def test_a_fresh_pull_replaces_an_older_one(store):
    """Garmin revises a day's sleep and stress for a day or two, so the newest
    payload is the most accurate one."""
    store.put_day("2026-09-20", good_day(rhr=50))
    store.put_day("2026-09-20", good_day(rhr=47))
    assert store.get_day("2026-09-20")["stats"]["restingHeartRate"] == 47


def test_an_empty_pull_never_erases_a_good_day(store):
    """The rule the whole design rests on: a failed API call must not destroy
    data that was already collected."""
    store.put_day("2026-09-20", good_day())
    store.put_day("2026-09-20", {"stats": {}, "sleep": {}, "_errors": {"sleep": "timeout"}})
    assert store.get_day("2026-09-20")["stats"]["restingHeartRate"] == 50


def test_an_empty_pull_is_stored_when_there_was_nothing_before(store):
    store.put_day("2026-09-20", {"stats": {}, "_errors": {"sleep": "timeout"}})
    assert store.get_day("2026-09-20") is not None


def test_writing_one_day_leaves_the_others_alone(store):
    """The original bug: writing the store wiped everything outside the window."""
    for day in ("2026-09-18", "2026-09-19", "2026-09-20"):
        store.put_day(day, good_day())
    store.put_day("2026-09-20", good_day(rhr=45))
    assert len(store.all_days()) == 3
    assert store.get_day("2026-09-18")["stats"]["restingHeartRate"] == 50


def test_all_days_is_ordered(store):
    for day in ("2026-09-20", "2026-09-18", "2026-09-19"):
        store.put_day(day, good_day())
    assert list(store.all_days()) == ["2026-09-18", "2026-09-19", "2026-09-20"]


def test_a_corrupt_file_is_skipped_not_fatal(store):
    store.put_day("2026-09-19", good_day())
    (store.DAYS_DIR / "2026-09-20.json").write_text("{ this is not json")
    days = store.all_days()
    assert "2026-09-19" in days and "2026-09-20" not in days


def test_has_day_reports_content_not_mere_existence(store):
    assert store.has_day("2026-09-20") is False
    store.put_day("2026-09-20", {"stats": {}})
    assert store.has_day("2026-09-20") is False      # a file, but nothing in it
    store.put_day("2026-09-20", good_day())
    assert store.has_day("2026-09-20") is True


def test_a_day_marked_failed_counts_as_held(store):
    """So a backfill doesn't retry a day Garmin has nothing for, forever."""
    store.put_day("2026-01-01", {"stats": {}, "_failed": True})
    assert store.has_day("2026-01-01") is True


def test_a_write_is_atomic(store):
    """Written via temp-file-and-rename, so a crash can't leave a half file."""
    store.put_day("2026-09-20", good_day())
    leftovers = list(store.DAYS_DIR.glob("*.tmp"))
    assert leftovers == []
    json.loads((store.DAYS_DIR / "2026-09-20.json").read_text())


# --------------------------------------------------------------------------
# activities
# --------------------------------------------------------------------------

def activity(activity_id, day="2026-09-20"):
    return {"activityId": activity_id, "startTimeLocal": f"{day} 07:00:00", "duration": 3600}


def test_activities_are_idempotent_by_id(store):
    """Re-pulling the same window must not duplicate what it already holds."""
    for _ in range(3):
        store.put_activity(activity(123))
    assert len(store.all_activities()) == 1


def test_activities_without_an_id_are_ignored(store):
    store.put_activity({"startTimeLocal": "2026-09-20 07:00:00"})
    assert store.all_activities() == []


def test_activities_come_back_in_time_order(store):
    store.put_activity(activity(2, "2026-09-21"))
    store.put_activity(activity(1, "2026-09-19"))
    assert [a["activityId"] for a in store.all_activities()] == [1, 2]


def test_activity_date_parses_both_formats(store):
    assert store.activity_date({"startTimeLocal": "2026-09-20 07:00:00"}) == date(2026, 9, 20)
    assert store.activity_date({"startTimeLocal": "2026-09-20T07:00:00"}) == date(2026, 9, 20)
    assert store.activity_date({"startTimeLocal": ""}) is None
    assert store.activity_date({}) is None


# --------------------------------------------------------------------------
# snapshots and manual entries
# --------------------------------------------------------------------------

def test_snapshots_build_a_trend_from_point_in_time_values(store):
    """These endpoints return only 'now', so keeping each observation is the
    only way a trend exists at all."""
    store.put_snapshot("max_metrics", "2026-09-01", {"v": 53})
    store.put_snapshot("max_metrics", "2026-09-20", {"v": 55})
    assert list(store.snapshots("max_metrics")) == ["2026-09-01", "2026-09-20"]
    assert store.latest_snapshot("max_metrics") == {"v": 55}


def test_an_empty_snapshot_is_not_stored(store):
    store.put_snapshot("race_predictions", "2026-09-20", {})
    assert store.snapshots("race_predictions") == {}
    assert store.latest_snapshot("race_predictions") is None


def test_manual_entries_merge_rather_than_replace(store):
    """Logging weight must not wipe the sleep logged earlier the same day."""
    store.put_manual("2026-09-20", {"sleep_hours": 7.5})
    store.put_manual("2026-09-20", {"weight_lb": 179})
    entry = store.get_manual("2026-09-20")
    assert entry["sleep_hours"] == 7.5 and entry["weight_lb"] == 179


def test_manual_entries_live_apart_from_measured_days(store):
    """They must never be mistaken for measurements, nor destroyed by a sync."""
    store.put_manual("2026-09-20", {"sleep_hours": 7.5})
    store.put_day("2026-09-20", good_day())
    assert store.get_manual("2026-09-20")["sleep_hours"] == 7.5
    assert "sleep_hours" not in store.get_day("2026-09-20")


def test_replacing_a_manual_entry_with_nothing_removes_it(store):
    store.put_manual("2026-09-20", {"sleep_hours": 7.5})
    store.replace_manual("2026-09-20", {})
    assert store.get_manual("2026-09-20") is None


def test_coverage_summarises_everything(store):
    store.put_day("2026-09-19", good_day())
    store.put_day("2026-09-20", good_day())
    store.put_activity(activity(1, "2026-09-19"))
    store.put_manual("2026-09-20", {"sleep_hours": 7})
    store.put_snapshot("max_metrics", "2026-09-20", {"v": 55})
    c = store.coverage()
    assert c["day_count"] == 2
    assert c["first_day"] == "2026-09-19" and c["last_day"] == "2026-09-20"
    assert c["activity_count"] == 1
    assert c["manual_entries"] == 1
    assert c["snapshots"]["max_metrics"] == 1
