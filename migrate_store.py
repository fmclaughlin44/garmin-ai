"""
One-time backfill of the persistent store from whatever the old pipeline left
behind.

Two sources, in order of fidelity:

1. ``garmin/data.json`` -- the old rolling snapshot. Full-fidelity API payloads,
   but only ever the last ~10 days, because each sync overwrote it.

2. ``garmin/wellness/*.md`` and ``garmin/workouts/*.md`` -- the human-readable
   notes the old sync wrote alongside the snapshot. These were never deleted, so
   they are the only surviving record of everything older. They are lossy (a
   rendered summary, not the raw payload), so we parse out the handful of fields
   the signal layer actually needs and mark each reconstructed record with
   ``"_source": "legacy_markdown"`` so nothing downstream mistakes a recovered
   summary for a real API payload.

Markdown-derived records never overwrite a record already in the store: raw
payloads always win. Safe to re-run.
"""

import json
import re
import sys
from pathlib import Path

import store

BASE = Path(__file__).resolve().parent
OLD_DATA_JSON = BASE / "garmin" / "data.json"
WELLNESS_DIR = BASE / "garmin" / "wellness"
WORKOUTS_DIR = BASE / "garmin" / "workouts"


def migrate_data_json() -> tuple:
    if not OLD_DATA_JSON.exists():
        return 0, 0
    try:
        data = json.loads(OLD_DATA_JSON.read_text())
    except Exception as e:
        print(f"  could not read {OLD_DATA_JSON.name}: {e}")
        return 0, 0

    days = 0
    for day_str, payload in (data.get("days") or {}).items():
        store.put_day(day_str, payload)
        days += 1

    acts = 0
    for activity in data.get("activities") or []:
        store.put_activity(activity)
        acts += 1

    return days, acts


# --------------------------------------------------------------------------
# Legacy markdown recovery
# --------------------------------------------------------------------------

def _num(text, pattern, cast=float):
    m = re.search(pattern, text)
    if not m:
        return None
    try:
        return cast(m.group(1))
    except Exception:
        return None


def _hm_to_seconds(text, pattern):
    """Parse a '7h 30m' / '45m' duration back into seconds."""
    m = re.search(pattern, text)
    if not m:
        return None
    chunk = m.group(1)
    hours = re.search(r"(\d+)h", chunk)
    mins = re.search(r"(\d+)m", chunk)
    total = 0
    if hours:
        total += int(hours.group(1)) * 3600
    if mins:
        total += int(mins.group(1)) * 60
    return total or None


def parse_wellness_md(text: str) -> dict:
    """Rebuild a minimal day payload from a rendered wellness note."""
    stats, sleep_dto = {}, {}

    total = _hm_to_seconds(text, r"Total sleep:\s*([0-9hm ]+)")
    if total:
        sleep_dto["sleepTimeSeconds"] = total
    for label, key in (("Deep", "deepSleepSeconds"), ("Light", "lightSleepSeconds"),
                       ("REM", "remSleepSeconds"), ("Awake", "awakeSleepSeconds")):
        secs = _hm_to_seconds(text, rf"{label}:\s*([0-9hm ]+)")
        if secs:
            sleep_dto[key] = secs
    score = _num(text, r"Sleep score:\s*(\d+)", int)
    if score is not None:
        sleep_dto["sleepScores"] = {"overall": {"value": score}}

    rhr = _num(text, r"##\s*Resting Heart Rate\s*\n-\s*(\d+)\s*bpm", int)
    if rhr is not None:
        stats["restingHeartRate"] = rhr

    for pattern, key in ((r"Charged:\s*(\d+)", "bodyBatteryChargedValue"),
                         (r"Drained:\s*(\d+)", "bodyBatteryDrainedValue"),
                         (r"Highest:\s*(\d+)", "bodyBatteryHighestValue"),
                         (r"Lowest:\s*(\d+)", "bodyBatteryLowestValue")):
        value = _num(text, pattern, int)
        if value is not None:
            stats[key] = value

    avg_stress = _num(text, r"##\s*Stress\s*\n-\s*Average:\s*(\d+)", int)
    if avg_stress is not None:
        stats["averageStressLevel"] = avg_stress
    max_stress = _num(text, r"Max:\s*(\d+)", int)
    if max_stress is not None:
        stats["maxStressLevel"] = max_stress

    steps = _num(text, r"-\s*([\d,]+)\s*steps", lambda s: int(s.replace(",", "")))
    if steps is not None:
        stats["totalSteps"] = steps

    payload = {"_source": "legacy_markdown"}
    if stats:
        payload["stats"] = stats
    if sleep_dto:
        payload["sleep"] = {"dailySleepDTO": sleep_dto}
    return payload


def parse_workout_md(path: Path, text: str) -> dict:
    """Rebuild a minimal activity payload from a rendered workout note.

    The filename carries the date and the Garmin activity id, which is what
    makes these recoverable at all: ``YYYY-MM-DD_Name_<activityId>.md``.
    """
    m = re.match(r"(\d{4}-\d{2}-\d{2})_(.*)_(\d+)$", path.stem)
    if not m:
        return {}
    day_str, _, activity_id = m.groups()

    name = None
    first = re.search(r"^#\s*(.+)$", text, re.M)
    if first:
        name = first.group(1).strip()
    type_key = None
    tm = re.search(r"^_(\w[\w ]*?)\s*—", text, re.M)
    if tm:
        type_key = tm.group(1).strip()

    duration = _hm_to_seconds(text, r"Duration:\s*([0-9hm ]+)")
    distance_km = _num(text, r"Distance:\s*([\d.]+)\s*km")
    calories = _num(text, r"Calories:\s*(\d+)", int)
    elev = _num(text, r"Elevation gain:\s*([\d.]+)\s*m")
    avg_hr = _num(text, r"##\s*Heart Rate\s*\n-\s*Average:\s*(\d+)", int)
    max_hr = _num(text, r"Max:\s*(\d+)\s*bpm", int)
    aerobic = _num(text, r"Aerobic:\s*([\d.]+)")
    anaerobic = _num(text, r"Anaerobic:\s*([\d.]+)")

    payload = {
        "_source": "legacy_markdown",
        "activityId": int(activity_id),
        "activityName": name,
        "startTimeLocal": f"{day_str} 00:00:00",
        "duration": duration,
        "calories": calories,
        "averageHR": avg_hr,
        "maxHR": max_hr,
        "aerobicTrainingEffect": aerobic,
        "anaerobicTrainingEffect": anaerobic,
    }
    if distance_km is not None:
        payload["distance"] = distance_km * 1000
    if elev is not None:
        payload["elevationGain"] = elev
    if type_key:
        payload["activityType"] = {"typeKey": type_key.strip().lower().replace(" ", "_")}
    return {k: v for k, v in payload.items() if v is not None}


def migrate_markdown() -> tuple:
    recovered_days = 0
    if WELLNESS_DIR.exists():
        for path in sorted(WELLNESS_DIR.glob("*.md")):
            if store.get_day(path.stem) is not None:
                continue  # raw payload already present -- always wins
            payload = parse_wellness_md(path.read_text())
            if len(payload) > 1:  # more than just _source
                store.put_day(path.stem, payload)
                recovered_days += 1

    existing_ids = {a.get("activityId") for a in store.all_activities()}
    recovered_acts = 0
    if WORKOUTS_DIR.exists():
        for path in sorted(WORKOUTS_DIR.glob("*.md")):
            payload = parse_workout_md(path, path.read_text())
            if not payload or payload.get("activityId") in existing_ids:
                continue
            store.put_activity(payload)
            existing_ids.add(payload["activityId"])
            recovered_acts += 1

    return recovered_days, recovered_acts


def main() -> None:
    store.ensure_dirs()

    print("Importing raw payloads from the old data.json snapshot...")
    days, acts = migrate_data_json()
    print(f"  {days} day(s), {acts} activity/activities")

    print("Recovering older history from the legacy markdown notes...")
    md_days, md_acts = migrate_markdown()
    print(f"  {md_days} day(s), {md_acts} activity/activities reconstructed")

    print("\nStore now holds:")
    for key, value in store.coverage().items():
        print(f"  {key}: {value}")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        import traceback
        traceback.print_exc()
        sys.exit(1)
