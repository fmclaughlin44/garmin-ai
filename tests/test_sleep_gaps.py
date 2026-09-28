"""
Tests for filling nights the watch missed.

The risk here is subtle: an estimate that silently passes for a measurement.
Sleep and the nocturnal drop carry 45% of the readiness weight between them, so
a hand-typed number quietly replacing a measured one — or a stage breakdown
invented from a typical ratio — would corrupt the score while looking fine.
"""

import sys
import zipfile
from datetime import date, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import import_apple_health as ah  # noqa: E402
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


# --------------------------------------------------------------------------
# Apple Health import
# --------------------------------------------------------------------------

def write_export(tmp_path, records, as_zip=False):
    body = "\n".join(
        f'<Record type="HKCategoryTypeIdentifierSleepAnalysis" sourceName="{src}" '
        f'startDate="{start}" endDate="{end}" value="{value}"/>'
        for src, start, end, value in records)
    xml = f'<?xml version="1.0" encoding="UTF-8"?>\n<HealthData>\n{body}\n</HealthData>\n'
    path = tmp_path / "export.xml"
    path.write_text(xml)
    if not as_zip:
        return path
    zip_path = tmp_path / "export.zip"
    with zipfile.ZipFile(zip_path, "w") as archive:
        archive.writestr("apple_health_export/export.xml", xml)
    return zip_path


ASLEEP = "HKCategoryValueSleepAnalysisAsleepCore"


def test_a_simple_night_totals_correctly(tmp_path):
    path = write_export(tmp_path, [
        ("iPhone", "2026-09-19 23:00:00 -0400", "2026-09-20 06:30:00 -0400", ASLEEP)])
    assert ah.extract_sleep(path)["2026-09-20"] == pytest.approx(7.5 * 3600)


def test_overlapping_records_are_merged_not_summed(tmp_path):
    """Apple writes a broad 'asleep' record plus per-stage records covering the
    same minutes. Summing them reports ten hours for a seven-hour night."""
    path = write_export(tmp_path, [
        ("iPhone", "2026-09-19 23:00:00 -0400", "2026-09-20 06:30:00 -0400", ASLEEP),
        ("Watch", "2026-09-19 23:05:00 -0400", "2026-09-20 01:00:00 -0400",
         "HKCategoryValueSleepAnalysisAsleepDeep"),
        ("Watch", "2026-09-20 01:00:00 -0400", "2026-09-20 03:00:00 -0400",
         "HKCategoryValueSleepAnalysisAsleepREM"),
    ])
    assert ah.extract_sleep(path)["2026-09-20"] == pytest.approx(7.5 * 3600)


def test_in_bed_records_are_excluded(tmp_path):
    """'In bed' overstates sleep, often by an hour. An inflated number is worse
    than a missing one for something feeding a readiness score."""
    path = write_export(tmp_path, [
        ("iPhone", "2026-09-19 22:00:00 -0400", "2026-09-19 23:00:00 -0400",
         "HKCategoryValueSleepAnalysisInBed"),
        ("iPhone", "2026-09-19 23:00:00 -0400", "2026-09-20 06:00:00 -0400", ASLEEP),
    ])
    assert ah.extract_sleep(path)["2026-09-20"] == pytest.approx(7 * 3600)


def test_a_night_is_filed_under_the_date_it_ended(tmp_path):
    """Garmin files a night under the wake date; this has to match or the two
    sources disagree by a day."""
    path = write_export(tmp_path, [
        ("iPhone", "2026-09-19 23:30:00 -0400", "2026-09-20 07:00:00 -0400", ASLEEP)])
    nights = ah.extract_sleep(path)
    assert "2026-09-20" in nights
    assert "2026-09-19" not in nights


def test_a_zip_export_is_read_directly(tmp_path):
    path = write_export(tmp_path, [
        ("iPhone", "2026-09-19 23:00:00 -0400", "2026-09-20 06:00:00 -0400", ASLEEP)],
        as_zip=True)
    assert ah.extract_sleep(path)["2026-09-20"] == pytest.approx(7 * 3600)


def test_non_sleep_records_are_ignored(tmp_path):
    xml = ('<?xml version="1.0" encoding="UTF-8"?>\n<HealthData>\n'
           '<Record type="HKQuantityTypeIdentifierStepCount" startDate="2026-09-20 08:00:00 -0400" '
           'endDate="2026-09-20 09:00:00 -0400" value="900"/>\n</HealthData>\n')
    path = tmp_path / "export.xml"
    path.write_text(xml)
    assert ah.extract_sleep(path) == {}


def test_a_malformed_timestamp_is_skipped_not_fatal(tmp_path):
    path = write_export(tmp_path, [
        ("iPhone", "not-a-date", "2026-09-20 06:00:00 -0400", ASLEEP),
        ("iPhone", "2026-09-19 23:00:00 -0400", "2026-09-20 06:00:00 -0400", ASLEEP)])
    assert ah.extract_sleep(path)["2026-09-20"] == pytest.approx(7 * 3600)
