"""
Fill in nights the watch missed.

A night spent charging is gone from Garmin permanently -- it is not backfilled
later, unlike sleep for a night the watch *did* record. Since sleep and the
nocturnal drop together carry 45% of the readiness weight, a missing night
meaningfully changes what the score is made of, and leaving it blank quietly
understates the inputs behind it.

What can and cannot be recovered by hand:

  sleep duration   yes -- type it in, or import it from Apple Health
  sleep stages     no  -- not recoverable, and not worth inventing from a ratio
  overnight HR     no  -- so the nocturnal drop stays unavailable for that night

Entries never overwrite measured data. If Garmin later reports that night, the
measurement wins and the manual entry is kept only as a record.

Usage:
  python3 log_sleep.py                      # walk the gaps interactively
  python3 log_sleep.py 2026-09-20 7.5       # log one night
  python3 log_sleep.py --gaps               # just list what's missing
  python3 log_sleep.py --clear 2026-09-20   # remove a manual entry
"""

import argparse
import sys
from datetime import date, datetime, timedelta

import signals
import store


def gaps(window_days=30):
    """Nights in the window with no sleep figure from Garmin or by hand."""
    days = store.all_days()
    manual = store.all_manual()
    today = date.today()
    start = (today - timedelta(days=window_days)).isoformat()

    out = []
    for day_str in sorted(days):
        if not (start <= day_str <= today.isoformat()):
            continue
        metrics = signals.extract_day_metrics(day_str, days[day_str], manual.get(day_str))
        if metrics["sleep_hours"] is None:
            out.append(day_str)
    return out


def log_night(day_str: str, hours: float, note: str = None, source: str = "manual"):
    try:
        datetime.fromisoformat(day_str)
    except ValueError:
        print(f"'{day_str}' is not a date in YYYY-MM-DD form.", file=sys.stderr)
        return False
    if not 0 < hours <= 24:
        print(f"{hours} is not a plausible number of hours.", file=sys.stderr)
        return False

    store.put_manual(day_str, {
        "sleep_hours": round(float(hours), 2),
        "note": note,
        "source": source,
        "logged_at": datetime.now().isoformat(timespec="seconds"),
    })
    print(f"  logged {hours}h for {day_str}")
    return True


def interactive(window_days=30):
    missing = gaps(window_days)
    if not missing:
        print(f"No missing nights in the last {window_days} days.")
        return

    print(f"{len(missing)} night(s) with no sleep data in the last {window_days} days.")
    print("Enter hours (e.g. 7.5), or press return to skip, or 'q' to stop.\n")

    logged = 0
    for day_str in missing:
        weekday = datetime.fromisoformat(day_str).strftime("%A")
        try:
            answer = input(f"  {weekday} {day_str}: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nStopped.")
            break
        if answer.lower() in ("q", "quit", "exit"):
            break
        if not answer:
            continue
        try:
            if log_night(day_str, float(answer)):
                logged += 1
        except ValueError:
            print("    not a number — skipped")

    print(f"\n{logged} night(s) logged. Run `python3 run_daily.py --skip-sync` to rebuild.")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("date", nargs="?", help="Night to log (YYYY-MM-DD)")
    parser.add_argument("hours", nargs="?", type=float, help="Hours slept")
    parser.add_argument("--note", help="Why it was missing, e.g. 'watch charging'")
    parser.add_argument("--gaps", action="store_true", help="List missing nights and exit")
    parser.add_argument("--clear", metavar="DATE", help="Remove a manual entry")
    parser.add_argument("--days", type=int, default=30, help="How far back to look")
    args = parser.parse_args()

    if args.clear:
        print("removed" if store.delete_manual(args.clear) else "no manual entry for that date")
        return

    if args.gaps:
        missing = gaps(args.days)
        if not missing:
            print(f"No missing nights in the last {args.days} days.")
            return
        print(f"{len(missing)} night(s) missing sleep data:")
        for day_str in missing:
            weekday = datetime.fromisoformat(day_str).strftime("%a")
            print(f"  {weekday} {day_str}   python3 log_sleep.py {day_str} <hours>")
        return

    if args.date and args.hours is not None:
        if log_night(args.date, args.hours, args.note):
            print("Run `python3 run_daily.py --skip-sync` to rebuild the dashboard.")
        return

    interactive(args.days)


if __name__ == "__main__":
    main()
