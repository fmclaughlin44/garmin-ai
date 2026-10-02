"""
The full daily pipeline. This is what the 6:30am schedule calls.

  sync_garmin.py   Garmin Connect  ->  garmin/store/  (additive)
  signals.py       store + plan    ->  web/data.json  (the output contract)
  adapt.py         web/data.json   ->  + plan matching and today's adjustments
  coach.py         web/data.json   ->  + the coach's read
  build_dashboard.py  data + template -> dashboard.html

Each stage reads the previous stage's output from disk and nothing else, so any
one of them can be run on its own while debugging.

The sync stage is allowed to fail without taking the rest down. Garmin's API
drops connections regularly, and a failed pull is not a reason to leave a stale
dashboard on screen: the store still holds yesterday's data, so the remaining
stages rebuild from that and the page says which day the data runs through.

Usage:
  python3 run_daily.py
  python3 run_daily.py --days 14 --live-coach
"""

import argparse
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import runlock

BASE = Path(__file__).resolve().parent
PY = str(BASE / ".venv" / "bin" / "python")
LOG_DIR = BASE / "logs"
MAX_LOG_BYTES = 512 * 1024
RUN_MARKER = "=== PIPELINE RUN START ==="


def rotate_logs() -> None:
    """Keep the launchd logs from growing without bound.

    launchd appends to StandardOutPath forever, so after a few months of daily
    runs the log is megabytes of mostly-identical output. Anything over the cap
    is moved aside to a single .1 file, which is enough history to debug a
    failure without keeping a year of successes.
    """
    for name in ("daily_sync.log", "daily_sync.err.log"):
        path = LOG_DIR / name
        try:
            if path.exists() and path.stat().st_size > MAX_LOG_BYTES:
                path.replace(path.with_suffix(path.suffix + ".1"))
        except OSError:
            pass  # a log we can't rotate is not a reason to skip the sync


def step(name: str, args: list, required: bool = True) -> bool:
    print(f"\n=== {name} ===", flush=True)
    result = subprocess.run([PY, *args], cwd=str(BASE))
    if result.returncode == 0:
        return True
    message = f"{name} failed (exit {result.returncode})"
    if required:
        print(message, file=sys.stderr)
        sys.exit(result.returncode)
    print(f"{message} — continuing with the data already in the store.", file=sys.stderr)
    return False


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=10, help="How many days back to sync")
    parser.add_argument("--live-coach", action="store_true",
                        help="Ask Claude for the coach note instead of the rules engine")
    parser.add_argument("--skip-sync", action="store_true",
                        help="Rebuild from the store without hitting Garmin")
    args = parser.parse_args()

    with runlock.exclusive("run_daily"):
        rotate_logs()
        header = f"Daily run — {datetime.now().strftime('%A %d %B %Y, %H:%M')}"
        print(f"\n{'=' * 62}\n{header}\n{'=' * 62}", flush=True)
        # The same marker on stderr, so healthcheck.py can count failures from
        # this run alone. The error log is append-only and holds months of
        # history, including failures from code that no longer exists.
        print(f"{RUN_MARKER} {header}", file=sys.stderr, flush=True)

        if not args.skip_sync:
            step("Sync Garmin", ["sync_garmin.py", "--days", str(args.days)], required=False)

        step("Compute signals", ["signals.py", "--print"])
        step("Match and adapt the plan", ["adapt.py"])
        step("Coach", ["coach.py"] + (["--live"] if args.live_coach else []))
    step("Build dashboard", ["build_dashboard.py"])
    # Last, so it judges what was actually produced. Never fatal: a failed
        # health check must not stop the dashboard from being written.
    step("Health check", ["healthcheck.py"], required=False)
    step("Rebuild with health", ["build_dashboard.py"], required=False)

    print(f"\nDone. Open {BASE / 'dashboard.html'}")


if __name__ == "__main__":
    main()
