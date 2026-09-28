"""
Imports sleep from an Apple Health export, to fill nights the watch missed.

How to get the file: on the iPhone, Health app -> your profile picture ->
Export All Health Data. That produces `export.zip`; AirDrop it to the Mac and
point this script at either the zip or the `export.xml` inside it.

What this can and cannot give you:

  sleep duration   yes -- iPhone tracks time asleep even with the watch charging
  sleep stages     sometimes -- only if a device recorded them (an Apple Watch
                   will; an iPhone alone reports in-bed/asleep only)
  overnight HR     no -- so the nocturnal drop stays unavailable for those nights

Imported nights land in the same manual store as hand-typed ones and are tagged
`apple_health`, so the dashboard can show them as imported rather than measured
by the Garmin. Measured Garmin data always wins where both exist.

A note on attribution: Apple records sleep as intervals with start and end
timestamps, while Garmin files a night under the date you woke up. This follows
Garmin's convention so the two are comparable.

Exports are routinely hundreds of megabytes, so the XML is streamed rather than
loaded.

Usage:
  python3 import_apple_health.py ~/Downloads/export.zip
  python3 import_apple_health.py ~/Downloads/apple_health_export/export.xml --dry-run
"""

import argparse
import sys
import zipfile
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from xml.etree import ElementTree

import store

SLEEP_TYPE = "HKCategoryTypeIdentifierSleepAnalysis"

# Values that mean actually asleep. "InBed" is deliberately excluded: it
# overstates sleep, often by an hour or more, and an inflated number is worse
# than a missing one for a signal that feeds a readiness score.
ASLEEP_VALUES = {
    "HKCategoryValueSleepAnalysisAsleep",
    "HKCategoryValueSleepAnalysisAsleepUnspecified",
    "HKCategoryValueSleepAnalysisAsleepCore",
    "HKCategoryValueSleepAnalysisAsleepDeep",
    "HKCategoryValueSleepAnalysisAsleepREM",
}


def parse_timestamp(value: str):
    """Apple writes '2026-09-20 23:12:00 -0400'."""
    try:
        return datetime.strptime(value, "%Y-%m-%d %H:%M:%S %z")
    except (ValueError, TypeError):
        return None


def open_export(path: Path):
    """Yield a file object for export.xml, from either the zip or the xml itself."""
    if path.suffix.lower() == ".zip":
        archive = zipfile.ZipFile(path)
        names = [n for n in archive.namelist() if n.endswith("export.xml")]
        if not names:
            raise SystemExit(f"No export.xml inside {path.name}")
        return archive.open(names[0])
    return open(path, "rb")


def extract_sleep(path: Path) -> dict:
    """Total asleep seconds per night, keyed by the date sleep ended.

    Intervals are merged before summing. Apple frequently writes overlapping
    records for the same minutes -- one per source device, plus a stage record
    and a broader 'asleep' record covering it -- and adding those up naively
    reports ten hours of sleep for a seven-hour night.
    """
    intervals = defaultdict(list)

    with open_export(path) as handle:
        for event, element in ElementTree.iterparse(handle, events=("end",)):
            if element.tag != "Record":
                element.clear()
                continue
            if element.get("type") != SLEEP_TYPE:
                element.clear()
                continue
            if element.get("value") not in ASLEEP_VALUES:
                element.clear()
                continue

            start = parse_timestamp(element.get("startDate"))
            end = parse_timestamp(element.get("endDate"))
            element.clear()
            if not start or not end or end <= start:
                continue
            # Filed under the wake date, matching Garmin's convention.
            intervals[end.date().isoformat()].append((start, end))

    nights = {}
    for day_str, spans in intervals.items():
        spans.sort()
        merged = []
        for start, end in spans:
            if merged and start <= merged[-1][1]:
                merged[-1] = (merged[-1][0], max(merged[-1][1], end))
            else:
                merged.append((start, end))
        seconds = sum((end - start).total_seconds() for start, end in merged)
        nights[day_str] = seconds
    return nights


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("export", help="Path to export.zip or export.xml")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be imported")
    parser.add_argument("--min-hours", type=float, default=2.0,
                        help="Ignore nights shorter than this (naps, stray records)")
    parser.add_argument("--all", action="store_true",
                        help="Import every night, not only the ones Garmin is missing")
    args = parser.parse_args()

    path = Path(args.export).expanduser()
    if not path.exists():
        raise SystemExit(f"{path} does not exist")

    print(f"Reading {path.name} (this can take a minute on a large export)...")
    nights = extract_sleep(path)
    print(f"  found {len(nights)} night(s) of sleep records")

    days = store.all_days()
    manual = store.all_manual()

    imported, skipped_short, skipped_have = 0, 0, 0
    for day_str in sorted(nights):
        hours = nights[day_str] / 3600
        if hours < args.min_hours:
            skipped_short += 1
            continue

        if not args.all:
            payload = days.get(day_str) or {}
            garmin_sleep = (((payload.get("sleep") or {}).get("dailySleepDTO") or {})
                            .get("sleepTimeSeconds"))
            if garmin_sleep:
                skipped_have += 1
                continue

        if args.dry_run:
            existing = " (replacing a manual entry)" if day_str in manual else ""
            print(f"  would import {day_str}: {hours:.2f}h{existing}")
        else:
            store.put_manual(day_str, {
                "sleep_hours": round(hours, 2),
                "source": "apple_health",
                "note": f"imported from {path.name}",
                "logged_at": datetime.now().isoformat(timespec="seconds"),
            })
        imported += 1

    print(f"\n{'Would import' if args.dry_run else 'Imported'} {imported} night(s).")
    if skipped_have:
        print(f"  {skipped_have} skipped — Garmin already has them (use --all to override)")
    if skipped_short:
        print(f"  {skipped_short} skipped — under {args.min_hours}h, likely naps")
    if imported and not args.dry_run:
        print("\nRun `python3 run_daily.py --skip-sync` to rebuild the dashboard.")
        print("Note: this fills sleep duration only. The overnight heart-rate series")
        print("isn't in an Apple Health export, so the nocturnal drop stays unavailable")
        print("for these nights.")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(130)
