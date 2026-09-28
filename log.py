"""
The manual input channel: things the watch can't measure, or missed.

Four kinds of entry, all stored per-day alongside the Garmin data but never
mixed into it:

  sleep      hours, for nights the watch spent on a charger. Gone from Garmin
             permanently -- unlike a recorded night, it is never backfilled.
  weight     bodyweight. There is no connected scale on this account, so every
             macro target scales off a number that only gets in this way, and in
             a deficit that number moves.
  soreness   morning muscle soreness, 1 (none) to 10 (wrecked).
  energy     subjective energy and motivation, 1 (flat) to 10 (flying).
  rpe        how hard the last session actually felt, 1 to 10.

Why subjective scores earn their place in a dashboard full of sensors:
perceived soreness and energy are among the better predictors of impending
injury and overreaching, and they are the only inputs here that see a problem
before the heart rate does. RPE also gives an independent measure of session
load -- RPE x minutes is Foster's session-RPE, which can be compared against the
heart-rate-derived TRIMP. When the two disagree, that disagreement is itself the
signal.

Entries never overwrite measured data. If Garmin later reports a night's sleep,
the measurement wins and the manual entry is kept only as a record.

Usage:
  python3 log.py                          # walk everything that's missing
  python3 log.py sleep 2026-09-20 7.5
  python3 log.py weight 2026-09-28 179.5
  python3 log.py rpe 2026-09-28 7
  python3 log.py soreness 2026-09-28 4 --note "calves after the long run"
  python3 log.py --gaps                   # what's missing
  python3 log.py --clear 2026-09-20 sleep # remove one field
"""

import argparse
import sys
from datetime import date, datetime, timedelta

import signals
import store

FIELDS = {
    "sleep":    {"key": "sleep_hours", "prompt": "hours slept", "low": 0.5, "high": 24,
                 "help": "e.g. 7.5"},
    "weight":   {"key": "weight_lb", "prompt": "bodyweight (lb)", "low": 50, "high": 500,
                 "help": "e.g. 179.5"},
    "soreness": {"key": "soreness", "prompt": "soreness 1-10", "low": 1, "high": 10,
                 "help": "1 = none, 10 = wrecked"},
    "energy":   {"key": "energy", "prompt": "energy 1-10", "low": 1, "high": 10,
                 "help": "1 = flat, 10 = flying"},
    "rpe":      {"key": "rpe", "prompt": "session RPE 1-10", "low": 1, "high": 10,
                 "help": "how hard the last session felt"},
}


def sleep_gaps(window_days=30):
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


def latest_weight():
    manual = store.all_manual()
    for day_str in sorted(manual, reverse=True):
        if manual[day_str].get("weight_lb"):
            return day_str, manual[day_str]["weight_lb"]
    return None, None


def record(field: str, day_str: str, value: float, note: str = None,
           source: str = "manual") -> bool:
    spec = FIELDS.get(field)
    if not spec:
        print(f"Unknown field '{field}'. One of: {', '.join(FIELDS)}", file=sys.stderr)
        return False
    try:
        datetime.fromisoformat(day_str)
    except ValueError:
        print(f"'{day_str}' is not a date in YYYY-MM-DD form.", file=sys.stderr)
        return False
    if not spec["low"] <= value <= spec["high"]:
        print(f"{value} is outside the plausible range for {field} "
              f"({spec['low']}-{spec['high']}).", file=sys.stderr)
        return False

    payload = {spec["key"]: round(float(value), 2),
               "source": source,
               "logged_at": datetime.now().isoformat(timespec="seconds")}
    if note:
        payload["note"] = note
    store.put_manual(day_str, payload)
    print(f"  {field} = {value} logged for {day_str}")
    return True


def show_gaps(window_days=30):
    missing = sleep_gaps(window_days)
    day_str, weight = latest_weight()
    today = date.today().isoformat()
    manual_today = store.get_manual(today) or {}

    if missing:
        print(f"{len(missing)} night(s) missing sleep in the last {window_days} days:")
        for m in missing:
            weekday = datetime.fromisoformat(m).strftime("%a")
            print(f"  {weekday} {m}   python3 log.py sleep {m} <hours>")
    else:
        print(f"No missing sleep in the last {window_days} days.")

    print()
    if weight:
        age = (date.today() - datetime.fromisoformat(day_str).date()).days
        stale = "  (stale — macros scale off this)" if age > 14 else ""
        print(f"Latest weight: {weight} lb, logged {day_str} ({age}d ago){stale}")
    else:
        print("No bodyweight logged. Macros fall back to config.json.")
        print("  python3 log.py weight " + today + " <lb>")

    print()
    subjective = [f for f in ("soreness", "energy", "rpe")
                  if FIELDS[f]["key"] not in manual_today]
    if subjective:
        print(f"Not logged today: {', '.join(subjective)}")
        print("  python3 log.py    (walks them interactively)")
    else:
        print("Subjective scores logged for today.")


def ask(field: str, day_str: str, label: str = None) -> bool:
    spec = FIELDS[field]
    weekday = datetime.fromisoformat(day_str).strftime("%a")
    prompt = f"  {label or weekday + ' ' + day_str} — {spec['prompt']} ({spec['help']}): "
    try:
        answer = input(prompt).strip()
    except (EOFError, KeyboardInterrupt):
        raise
    if not answer:
        return False
    if answer.lower() in ("q", "quit", "exit"):
        raise KeyboardInterrupt
    try:
        return record(field, day_str, float(answer))
    except ValueError:
        print("    not a number — skipped")
        return False


def interactive(window_days=30):
    today = date.today().isoformat()
    logged = 0

    print("Press return to skip anything, or 'q' to stop.\n")
    try:
        missing = sleep_gaps(window_days)
        if missing:
            print(f"Sleep — {len(missing)} night(s) the watch didn't record:")
            for day_str in missing:
                if ask("sleep", day_str):
                    logged += 1
            print()

        manual_today = store.get_manual(today) or {}
        print(f"Today ({today}):")
        for field in ("weight", "soreness", "energy", "rpe"):
            if FIELDS[field]["key"] in manual_today:
                print(f"  {field} already logged ({manual_today[FIELDS[field]['key']]})")
                continue
            if ask(field, today, label="today"):
                logged += 1
    except KeyboardInterrupt:
        print("\nStopped.")

    print(f"\n{logged} value(s) logged. "
          "Run `python3 run_daily.py --skip-sync` to rebuild the dashboard.")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("field", nargs="?", choices=list(FIELDS), help="What to log")
    parser.add_argument("date", nargs="?", help="Date (YYYY-MM-DD)")
    parser.add_argument("value", nargs="?", type=float, help="The value")
    parser.add_argument("--note", help="Free text, e.g. 'watch charging'")
    parser.add_argument("--gaps", action="store_true", help="Show what's missing and exit")
    parser.add_argument("--clear", metavar="DATE", help="Remove an entry for this date")
    parser.add_argument("--field", dest="clear_field",
                        help="With --clear, remove only this field")
    parser.add_argument("--days", type=int, default=30, help="How far back to look")
    args = parser.parse_args()

    if args.clear:
        if args.clear_field:
            entry = store.get_manual(args.clear) or {}
            key = FIELDS.get(args.clear_field, {}).get("key")
            if key and key in entry:
                del entry[key]
                store.replace_manual(args.clear, entry)
                print(f"removed {args.clear_field} for {args.clear}")
            else:
                print("nothing to remove")
        else:
            print("removed" if store.delete_manual(args.clear) else "no entry for that date")
        return

    if args.gaps:
        show_gaps(args.days)
        return

    if args.field and args.date and args.value is not None:
        if record(args.field, args.date, args.value, args.note):
            print("Run `python3 run_daily.py --skip-sync` to rebuild the dashboard.")
        return

    if args.field and not args.date:
        parser.error(f"give a date and value, e.g. python3 log.py {args.field} "
                     f"{date.today().isoformat()} <value>")

    interactive(args.days)


if __name__ == "__main__":
    main()
