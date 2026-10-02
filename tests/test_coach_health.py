"""
Tests for the coach's rules engine and the pipeline health check.

The coach is the part of the dashboard that speaks in sentences, which makes it
the easiest place to say something confidently wrong. The two failure modes worth
pinning are quoting one model's number beside another model's gap, and shouting
about a pace deficit that is simply what the start of a training block looks like.

The health check has the opposite risk: staying quiet. It exists because the
Garmin token expires, syncs fail, and the dashboard keeps rendering stale numbers
as though nothing happened.
"""

import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import coach  # noqa: E402
import healthcheck  # noqa: E402


def payload(**overrides):
    base = {
        "readiness": {"score": 80, "band": "good"},
        "recovery": {"resting_hr": {"today": 48, "baseline": 48.0, "delta": 0.0, "z": 0.0},
                     "sleep": {"avg_7day_hours": 7.8, "target_hours": 7.5, "debt_7day_hours": 0}},
        "load": {"zone": "optimal", "ratio": 1.0, "monotony": 1.2},
        "adherence": {"completion_pct": 100, "key_sessions_due_to_date": 3,
                      "key_sessions_done_to_date": 3},
        "projection": {"predicted": "4:02:45", "best": "3:50:15", "gap_seconds": 300,
                       "garmin": {"marathon": "3:50:15"}},
        "race": {"goal": "Sub-3:45", "days_to_race": 48},
        "today": {"plan": {"session": "Long Run 16mi"}},
    }
    for key, value in overrides.items():
        base[key] = value
    return base


# --------------------------------------------------------------------------
# the coach
# --------------------------------------------------------------------------

def test_a_clean_day_reads_as_good():
    note = coach.build_rules_note(payload())
    assert note["band"] == "good"
    assert note["source"] == "rules"


def test_an_elevated_resting_heart_rate_is_the_headline():
    note = coach.build_rules_note(payload(recovery={
        "resting_hr": {"today": 55, "baseline": 48.0, "delta": 7.0, "z": 2.3},
        "sleep": {"avg_7day_hours": 7.8, "target_hours": 7.5}}))
    assert note["band"] in ("low", "poor")
    assert any("resting hr" in n.lower() for n in note["notes"])


def test_the_projection_note_names_both_models():
    """Quoting Garmin's gap beside Riegel's time is how a dashboard starts lying
    quietly. Both numbers, or neither."""
    note = coach.build_rules_note(payload())
    line = next(n for n in note["notes"] if "3:50:15" in n or "4:02:45" in n)
    assert "3:50:15" in line and "4:02:45" in line


def test_a_pace_gap_early_in_the_block_is_not_treated_as_a_crisis():
    """A 20-minute gap seven weeks out is the normal starting position."""
    early = coach.build_rules_note(payload(
        projection={"predicted": "4:05:00", "best": "4:05:00", "gap_seconds": 1200,
                    "garmin": {"marathon": "4:05:00"}},
        race={"goal": "Sub-3:45", "days_to_race": 60}))
    late = coach.build_rules_note(payload(
        projection={"predicted": "4:05:00", "best": "4:05:00", "gap_seconds": 1200,
                    "garmin": {"marathon": "4:05:00"}},
        race={"goal": "Sub-3:45", "days_to_race": 10}))
    order = {"good": 0, "moderate": 1, "low": 2, "poor": 3}
    assert order[late["band"]] > order[early["band"]]


def test_detraining_is_explained_rather_than_alarmed_about():
    note = coach.build_rules_note(payload(load={"zone": "detraining", "ratio": 0.6}))
    line = next(n for n in note["notes"] if "0.6" in n)
    assert "race" in line.lower() or "expected" in line.lower()


def test_a_load_spike_is_called_out():
    note = coach.build_rules_note(payload(load={"zone": "high_risk", "ratio": 1.8}))
    assert note["band"] in ("low", "poor")


def test_the_note_survives_a_completely_empty_payload():
    note = coach.build_rules_note({})
    assert note["band"] == "unknown"
    assert note["source"] == "rules"


def test_the_rules_engine_is_deterministic():
    data = payload()
    assert coach.build_rules_note(data) == coach.build_rules_note(data)


def test_the_live_path_falls_back_without_an_api_key(monkeypatch):
    """A missing key must degrade to the rules engine, never break the build."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    note = coach.build_claude_note(payload())
    assert note["source"] == "rules"
    assert "ANTHROPIC_API_KEY" in note["fallback_reason"]


# --------------------------------------------------------------------------
# health check
# --------------------------------------------------------------------------

def health_payload(days_stale=0, build_hours=1, missing_nights=0, inputs_used=6):
    through = (date.today() - timedelta(days=days_stale)).isoformat()
    built = (datetime.now() - timedelta(hours=build_hours)).isoformat(timespec="seconds")
    return {
        "meta": {"data_through": through, "generated_at": built,
                 "sleep_gaps": {"missing_count": missing_nights, "window_days": 30,
                                "nights_in_window": 30}},
        "readiness": {"inputs_used": ["a"] * inputs_used,
                      "inputs_missing": ["b"] * (7 - inputs_used)},
    }


def test_fresh_data_passes():
    assert healthcheck.check_freshness(health_payload())["status"] == "ok"


def test_data_a_few_days_old_warns():
    assert healthcheck.check_freshness(health_payload(days_stale=2))["status"] == "warn"


def test_very_stale_data_fails_and_names_the_likely_cause():
    """The actual failure this guards: an expired token, silently, for days."""
    result = healthcheck.check_freshness(health_payload(days_stale=6))
    assert result["status"] == "fail"
    assert "login.py" in result["detail"]


def test_an_old_build_warns_that_the_schedule_may_be_dead():
    assert healthcheck.check_build_age(health_payload(build_hours=100))["status"] == "warn"


def test_a_readiness_score_on_too_few_inputs_is_flagged():
    assert healthcheck.check_readiness_inputs(health_payload(inputs_used=2))["status"] == "fail"
    assert healthcheck.check_readiness_inputs(health_payload(inputs_used=4))["status"] == "warn"
    assert healthcheck.check_readiness_inputs(health_payload(inputs_used=7))["status"] == "ok"


def test_sleep_coverage_warns_only_past_a_real_gap_rate():
    assert healthcheck.check_sleep_coverage(health_payload(missing_nights=1))["status"] == "ok"
    assert healthcheck.check_sleep_coverage(health_payload(missing_nights=8))["status"] == "warn"


def test_only_the_most_recent_run_is_counted(tmp_path, monkeypatch):
    """The error log is append-only. Without scoping to the last run it keeps
    reporting failures from code that has since been replaced."""
    log = tmp_path / "err.log"
    log.write_text(
        "Daily run failed: Command '[old]' returned non-zero exit status 1.\n"
        "Daily run failed: Command '[old]' returned non-zero exit status 1.\n"
        f"{healthcheck.RUN_MARKER} Daily run — Thursday 01 October 2026, 06:30\n"
        "API call attempt 1/4 failed (path=/x status=504) — retrying\n")
    monkeypatch.setattr(healthcheck, "ERR_LOG", log)
    result = healthcheck.check_recent_failures()
    assert result["status"] == "ok", result["detail"]


def test_an_unscoped_log_does_not_warn(tmp_path, monkeypatch):
    """A log with no run marker predates per-run scoping, so its failures can't
    be attributed to a recent run — warning about them is unactionable."""
    log = tmp_path / "err.log"
    log.write_text("Daily run failed: Command '[old]' returned non-zero exit status 1.\n")
    monkeypatch.setattr(healthcheck, "ERR_LOG", log)
    assert healthcheck.check_recent_failures()["status"] == "ok"


def test_old_failures_do_not_warn_forever(tmp_path, monkeypatch):
    """A banner that is always on is a banner nobody reads. Failures from weeks
    ago must not keep the health check in WARN."""
    import os, time
    log = tmp_path / "err.log"
    log.write_text("Daily run failed: something\nDaily run failed: again\n")
    old = time.time() - 10 * 24 * 3600
    os.utime(log, (old, old))
    monkeypatch.setattr(healthcheck, "ERR_LOG", log)
    assert healthcheck.check_recent_failures()["status"] == "ok"


def test_a_real_stage_failure_warns(tmp_path, monkeypatch):
    log = tmp_path / "err.log"
    log.write_text(f"{healthcheck.RUN_MARKER} Daily run\n"
                   "Daily run failed: Command '[...]' returned non-zero exit status 1.\n")
    monkeypatch.setattr(healthcheck, "ERR_LOG", log)
    assert healthcheck.check_recent_failures()["status"] == "warn"


def test_transient_api_retries_are_not_failures(tmp_path, monkeypatch):
    """Garmin produces a lot of these — DNS blips while the laptop sleeps, 504s,
    dropped connections — and they retry and succeed. Counting them put the
    banner permanently in WARN over nothing."""
    log = tmp_path / "err.log"
    log.write_text(
        f"{healthcheck.RUN_MARKER} Daily run\n"
        "API call attempt 1/4 failed (path=/x status=504) — retrying in 0.8s\n"
        "API call network error attempt 2/4 (path=/y) — retrying in 1.0s\n")
    monkeypatch.setattr(healthcheck, "ERR_LOG", log)
    assert healthcheck.check_recent_failures()["status"] == "ok"


def test_a_sync_falling_back_to_stored_data_is_designed_behaviour(tmp_path, monkeypatch):
    """run_daily treats sync as non-required on purpose: the store still holds
    yesterday, so the dashboard rebuilds rather than going blank."""
    log = tmp_path / "err.log"
    log.write_text(f"{healthcheck.RUN_MARKER} Daily run\n"
                   "Sync Garmin failed (exit 1) — continuing with the data "
                   "already in the store.\n")
    monkeypatch.setattr(healthcheck, "ERR_LOG", log)
    result = healthcheck.check_recent_failures()
    assert result["status"] == "ok"
    assert "as designed" in result["detail"]


def test_overall_status_takes_the_worst_check(monkeypatch):
    monkeypatch.setattr(healthcheck, "check_store_growth",
                        lambda: {"name": "Store", "status": "ok", "detail": ""})
    monkeypatch.setattr(healthcheck, "check_recent_failures",
                        lambda: {"name": "Recent runs", "status": "ok", "detail": ""})
    result = healthcheck.run(health_payload(days_stale=6))
    assert result["status"] == "fail"
    assert any(c["status"] == "fail" for c in result["checks"])


def test_notification_fires_on_a_change_into_a_bad_state(tmp_path, monkeypatch):
    """Notify when things break, not every two hours forever."""
    monkeypatch.setattr(healthcheck, "STATE", tmp_path / "state.json")
    assert healthcheck.should_notify("warn") is True     # ok -> warn
    assert healthcheck.should_notify("warn") is False    # still warn, stay quiet
    assert healthcheck.should_notify("fail") is True     # warn -> fail
    assert healthcheck.should_notify("ok") is False      # recovered, no alarm
