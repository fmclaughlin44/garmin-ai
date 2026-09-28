"""
One-time historical backfill of the store.

The daily sync deliberately pulls a short trailing window, which is right for a
scheduled job and useless for getting history in. This walks a long date range
instead, and exists because the store began on 2026-08-12 (when the first version
of this project was written) while the Garmin account actually holds data back to
2025-12-26 -- nine months of it. Every 28-day baseline, the fitness curve, and the
workload ratio were all being computed against a fraction of the available history.

Built to be interrupted. Days already held are skipped, so a run that dies
halfway resumes where it left off, and a rate-limit response stops the run rather
than retrying into a ban. Expect roughly 7 API calls per day of history at 0.4s
spacing -- a nine-month backfill takes on the order of 20 minutes.

Usage:
  python3 backfill.py --start 2025-12-26
  python3 backfill.py --start 2025-12-26 --activities-only
"""

import argparse
import sys
import time
import traceback
from datetime import date, datetime, timedelta

import runlock
import store
from sync_garmin import (RateLimited, fetch_day, login, render_activity_markdown,
                         render_wellness_markdown, safe_call, safe_filename,
                         WELLNESS_DIR, WORKOUTS_DIR)


def log(msg):
    print(msg, flush=True)


def backfill_activities(garmin, page_size=100, max_pages=20):
    """Page through the whole activity history.

    get_activities takes an offset, and Garmin returns a short page when it runs
    out, so paging until a short page is the way to reach the beginning.
    """
    stored, start, pages = 0, 0, 0
    while pages < max_pages:
        batch, err = safe_call("activities", lambda: garmin.get_activities(start, page_size))
        if err:
            log(f"  activities page at offset {start}: {err}")
            break
        if not batch:
            break
        for activity in batch:
            store.put_activity(activity)
            stored += 1
            try:
                day_str, md = render_activity_markdown(activity)
                name = safe_filename(activity.get("activityName") or "workout")
                (WORKOUTS_DIR / f"{day_str}_{name}_{activity.get('activityId','x')}.md").write_text(md)
            except Exception:
                pass  # a note we can't render is not a reason to lose the activity
        log(f"  stored {stored} activities so far (offset {start})")
        if len(batch) < page_size:
            break
        start += page_size
        pages += 1
    return stored


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", required=True, help="Earliest date to pull (YYYY-MM-DD)")
    parser.add_argument("--end", help="Latest date to pull (defaults to today)")
    parser.add_argument("--activities-only", action="store_true",
                        help="Skip the per-day wellness walk")
    parser.add_argument("--force", action="store_true", help="Re-pull days already cached")
    args = parser.parse_args()

    with runlock.exclusive("backfill"):
        start = datetime.fromisoformat(args.start).date()
        end = datetime.fromisoformat(args.end).date() if args.end else date.today()

        WELLNESS_DIR.mkdir(parents=True, exist_ok=True)
        WORKOUTS_DIR.mkdir(parents=True, exist_ok=True)
        store.ensure_dirs()

        log("Logging in with saved token...")
        garmin = login()
        log("Logged in.")

        before = store.coverage()
        log(f"Store currently covers {before['first_day']} -> {before['last_day']} "
            f"({before['day_count']} days, {before['activity_count']} activities)")

        try:
            log("\nBackfilling activities...")
            stored = backfill_activities(garmin)
            log(f"  {stored} activities written")

            if not args.activities_only:
                days = [(start + timedelta(days=i)) for i in range((end - start).days + 1)]
                todo = [d for d in days if args.force or not store.has_day(d.isoformat())]
                log(f"\nBackfilling wellness: {len(todo)} of {len(days)} days need pulling")
                log(f"(roughly {len(todo) * 7 * 0.4 / 60:.0f} minutes at the configured pacing)")

                for index, day in enumerate(todo, 1):
                    iso = day.isoformat()
                    payload = fetch_day(garmin, iso)
                    if payload.get("_errors") and not any(
                            payload.get(k) not in (None, {}, []) for k in
                            ("stats", "sleep", "body_battery", "stress", "steps")):
                        # Nothing came back at all: mark it so future runs don't retry
                        # this day forever.
                        payload["_failed"] = True
                    store.put_day(iso, payload)
                    try:
                        (WELLNESS_DIR / f"{iso}.md").write_text(render_wellness_markdown(iso, payload))
                    except Exception:
                        pass
                    if index % 10 == 0 or index == len(todo):
                        log(f"  {index}/{len(todo)} days ({iso})")

        except RateLimited as e:
            log(f"\nGarmin rate-limited this run ({e}).")
            log("Stopping. Everything pulled so far is saved — re-run to resume.")
        except KeyboardInterrupt:
            log("\nInterrupted. Everything pulled so far is saved — re-run to resume.")

        after = store.coverage()
        log(f"\nStore now covers {after['first_day']} -> {after['last_day']} "
            f"({after['day_count']} days, {after['activity_count']} activities)")
        log(f"Gained {after['day_count'] - before['day_count']} days and "
            f"{after['activity_count'] - before['activity_count']} activities.")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        traceback.print_exc()
        sys.exit(1)
