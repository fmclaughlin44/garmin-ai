"""
Decides whether the pipeline is actually healthy, and says so loudly if not.

The failure mode this exists for: the Garmin token expires, every sync fails from
then on, run_daily carries on rebuilding the dashboard from stale data, and the
only evidence is a timestamp you have to open the page to notice. The error log
shows this has already happened repeatedly.

So the checks run at the end of every pipeline run, write their verdict into
web/data.json for the page to render, and raise a macOS notification when
something degrades. A dashboard that is confidently wrong is worse than one that
admits it is stale.

Usage:
  python3 healthcheck.py            # check, annotate web/data.json, notify
  python3 healthcheck.py --quiet    # no notification
"""

import argparse
import json
import subprocess
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import store

BASE = Path(__file__).resolve().parent
DATA_JSON = BASE / "web" / "data.json"
ERR_LOG = BASE / "logs" / "daily_sync.err.log"
STATE = BASE / ".tmp-health-state.json"

# How stale the data can get before it matters. The top-up runs every two hours,
# so a day without fresh data means several consecutive runs failed.
STALE_WARN_HOURS = 30
STALE_FAIL_HOURS = 72


def check_freshness(data: dict) -> dict:
    through = (data.get("meta") or {}).get("data_through")
    if not through:
        return {"name": "Data freshness", "status": "fail",
                "detail": "No data_through in the signals file."}
    age_days = (date.today() - datetime.fromisoformat(through).date()).days
    if age_days <= 1:
        return {"name": "Data freshness", "status": "ok",
                "detail": f"Data runs through {through}."}
    if age_days * 24 <= STALE_FAIL_HOURS:
        return {"name": "Data freshness", "status": "warn",
                "detail": f"Newest data is {age_days} days old ({through}). "
                          "A sync may be failing."}
    return {"name": "Data freshness", "status": "fail",
            "detail": f"Newest data is {age_days} days old ({through}). "
                      "The sync has been failing — the Garmin token may have expired; "
                      "run `python3 login.py`."}


def check_build_age(data: dict) -> dict:
    generated = (data.get("meta") or {}).get("generated_at")
    if not generated:
        return {"name": "Last build", "status": "fail", "detail": "No build timestamp."}
    age_hours = (datetime.now() - datetime.fromisoformat(generated)).total_seconds() / 3600
    if age_hours <= STALE_WARN_HOURS:
        return {"name": "Last build", "status": "ok",
                "detail": f"Rebuilt {age_hours:.0f}h ago."}
    return {"name": "Last build", "status": "warn",
            "detail": f"Last rebuilt {age_hours / 24:.1f} days ago — "
                      "the scheduled jobs may not be running."}


# Lines that mean a whole pipeline stage gave up, as opposed to a single API
# call retrying and then succeeding. The library logs "API call attempt 1/4
# failed" for every transient blip, and Garmin produces a lot of those: DNS
# failures while the laptop is asleep, 504s, dropped connections. Counting those
# as failures put the health banner permanently in WARN over problems that
# resolved themselves within a second.
STAGE_FAILURE_MARKERS = (
    "returned non-zero exit status",
    "failed (exit",
)
# This one means the sync gave up but the rest of the pipeline carried on from
# the store, which is the designed degradation rather than a breakage.
DEGRADED_MARKER = "continuing with the data already in the store"
# Written to stderr by run_daily at the start of each run.
RUN_MARKER = "=== PIPELINE RUN START ==="


def check_recent_failures(window_hours=48) -> dict:
    """Pipeline stages that gave up recently — not individual API retries.

    The error log is append-only and holds months of history, so the file's
    modification time gates the check: if nothing has been written recently,
    there are no recent failures to report.
    """
    if not ERR_LOG.exists():
        return {"name": "Recent runs", "status": "ok", "detail": "No error log."}

    age_hours = (datetime.now().timestamp() - ERR_LOG.stat().st_mtime) / 3600
    if age_hours > window_hours:
        return {"name": "Recent runs", "status": "ok",
                "detail": f"No errors logged in the last {int(age_hours / 24)} day(s)."}

    lines = ERR_LOG.read_text(errors="replace").splitlines()

    # Scope to the most recent run. Without this the check reads months of
    # append-only history and keeps reporting failures from code that has since
    # been replaced -- which is how it ended up warning about 17 failures from a
    # sync invocation that no longer exists.
    scoped = False
    for index in range(len(lines) - 1, -1, -1):
        if RUN_MARKER in lines[index]:
            lines = lines[index + 1:]
            scoped = True
            break

    if not scoped:
        # No marker means this log predates per-run scoping, so its failures
        # can't be attributed to any particular run. Reporting them would be
        # warning about the past with no way to act on it.
        return {"name": "Recent runs", "status": "ok",
                "detail": "No run marker in the log yet — nothing attributable "
                          "to a recent run."}

    hard, degraded = 0, 0
    for line in lines:
        if not any(marker in line for marker in STAGE_FAILURE_MARKERS):
            continue
        if DEGRADED_MARKER in line:
            degraded += 1
        else:
            hard += 1

    if not hard and not degraded:
        return {"name": "Recent runs", "status": "ok",
                "detail": "Last run clean — transient API retries only."}
    if not hard:
        return {"name": "Recent runs", "status": "ok",
                "detail": f"{degraded} sync(s) fell back to stored data, as designed. "
                          "Usually the laptop being offline at a scheduled time."}
    return {"name": "Recent runs", "status": "warn",
            "detail": f"{hard} stage failure(s) in the last run. "
                      "Check logs/daily_sync.err.log."}


def check_store_growth() -> dict:
    coverage = store.coverage()
    if not coverage.get("last_day"):
        return {"name": "Store", "status": "fail", "detail": "The store is empty."}
    age = (date.today() - datetime.fromisoformat(coverage["last_day"]).date()).days
    detail = (f"{coverage['day_count']} days "
              f"({coverage['first_day']} → {coverage['last_day']}), "
              f"{coverage['activity_count']} activities.")
    if age <= 1:
        return {"name": "Store", "status": "ok", "detail": detail}
    return {"name": "Store", "status": "warn",
            "detail": detail + f" Newest day is {age} days old."}


def check_sleep_coverage(data: dict) -> dict:
    gaps = (data.get("meta") or {}).get("sleep_gaps") or {}
    missing, window = gaps.get("missing_count", 0), gaps.get("window_days", 30)
    if not missing:
        return {"name": "Sleep coverage", "status": "ok",
                "detail": f"No missing nights in the last {window} days."}
    nights = gaps.get("nights_in_window") or window
    status = "warn" if missing / max(nights, 1) > 0.15 else "ok"
    return {"name": "Sleep coverage", "status": status,
            "detail": f"{missing} of {nights} nights missing sleep. "
                      "Fill them with `python3 log.py`."}


def check_readiness_inputs(data: dict) -> dict:
    """How much of the readiness model is actually firing today."""
    readiness = data.get("readiness") or {}
    used, missing = readiness.get("inputs_used") or [], readiness.get("inputs_missing") or []
    total = len(used) + len(missing)
    if not total:
        return {"name": "Readiness inputs", "status": "fail", "detail": "No inputs at all."}
    if len(used) >= total - 1:
        return {"name": "Readiness inputs", "status": "ok",
                "detail": f"{len(used)} of {total} inputs available."}
    if len(used) >= 3:
        return {"name": "Readiness inputs", "status": "warn",
                "detail": f"Only {len(used)} of {total} inputs today "
                          f"(missing: {', '.join(missing)})."}
    return {"name": "Readiness inputs", "status": "fail",
            "detail": f"Only {len(used)} of {total} inputs — the score is barely supported."}


def notify(title: str, message: str) -> None:
    """A macOS banner. Best effort: never let a failed notification fail the run."""
    try:
        subprocess.run(
            ["osascript", "-e",
             f'display notification {json.dumps(message)} with title {json.dumps(title)}'],
            capture_output=True, timeout=10)
    except Exception:
        pass


def should_notify(status: str) -> bool:
    """Notify on a change into a bad state, not every two hours forever."""
    previous = None
    try:
        previous = json.loads(STATE.read_text()).get("status")
    except Exception:
        pass
    try:
        STATE.write_text(json.dumps({"status": status,
                                     "at": datetime.now().isoformat(timespec="seconds")}))
    except OSError:
        pass
    return status in ("warn", "fail") and status != previous


def run(data: dict) -> dict:
    checks = [
        check_freshness(data),
        check_build_age(data),
        check_store_growth(),
        check_recent_failures(),
        check_sleep_coverage(data),
        check_readiness_inputs(data),
    ]
    order = {"ok": 0, "warn": 1, "fail": 2}
    overall = max((c["status"] for c in checks), key=lambda s: order[s])
    return {
        "status": overall,
        "checks": checks,
        "checked_at": datetime.now().isoformat(timespec="seconds"),
        "summary": {
            "ok": "Everything is current.",
            "warn": "Something needs attention — the numbers may be stale.",
            "fail": "The pipeline is not working. Treat what's on screen as out of date.",
        }[overall],
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--quiet", action="store_true", help="Skip the macOS notification")
    parser.add_argument("--data", default=str(DATA_JSON))
    args = parser.parse_args()

    path = Path(args.data)
    if not path.exists():
        print(f"{path} not found — run signals.py first.", file=sys.stderr)
        sys.exit(1)

    data = json.loads(path.read_text())
    health = run(data)
    data["health"] = health
    path.write_text(json.dumps(data, indent=2, default=str))

    icon = {"ok": "ok  ", "warn": "WARN", "fail": "FAIL"}
    print(f"Health: {health['status'].upper()} — {health['summary']}")
    for check in health["checks"]:
        print(f"  [{icon[check['status']]}] {check['name']}: {check['detail']}")

    if not args.quiet and should_notify(health["status"]):
        failing = [c for c in health["checks"] if c["status"] != "ok"]
        notify("Training dashboard: " + health["status"].upper(),
               failing[0]["detail"] if failing else health["summary"])


if __name__ == "__main__":
    main()
