"""
Pulls recent Garmin Connect data (workouts + daily wellness) into the
persistent store, and writes the plain-English notes alongside it:

  garmin/store/days/YYYY-MM-DD.json      raw wellness payloads, one file per day
  garmin/store/activities/<id>.json      raw activity payload, one file per activity
  garmin/wellness/YYYY-MM-DD.md          one readable note per day
  garmin/workouts/...                    one readable note per activity

Uses a saved login token in .tokens/ (created by login.py) so it never needs
your password after the first login.

Writes are additive: this only replaces the days it actually fetched and leaves
the rest of the history alone. (The original version rewrote a single
garmin/data.json on every run, which silently deleted anything older than the
sync window -- see store.py for why that mattered.)

Usage:
  python3 sync_garmin.py --days 3
"""

import argparse
import json
import sys
import traceback
from datetime import date, timedelta
from pathlib import Path

import garminconnect

import store

BASE = Path(__file__).resolve().parent
TOKENSTORE = BASE / ".tokens"
OUT_DIR = BASE / "garmin"
WELLNESS_DIR = OUT_DIR / "wellness"
WORKOUTS_DIR = OUT_DIR / "workouts"


def log(msg: str) -> None:
    print(msg, flush=True)


def login():
    if not TOKENSTORE.exists() or not any(TOKENSTORE.iterdir()):
        log("No saved login found. Please run login.py first.")
        sys.exit(1)
    try:
        garmin = garminconnect.Garmin()
        garmin.login(str(TOKENSTORE))
        return garmin
    except Exception as e:
        log(f"Could not log in using the saved token ({e}).")
        log("Your saved login may have expired -- please run login.py again.")
        sys.exit(1)


def safe_call(label, fn):
    try:
        return fn(), None
    except Exception as e:
        return None, f"{type(e).__name__}: {e}"


def get_path(d, *keys, default=None):
    cur = d
    for k in keys:
        if not isinstance(cur, dict):
            return default
        cur = cur.get(k)
        if cur is None:
            return default
    return cur


def seconds_to_hm(secs):
    if secs is None:
        return None
    secs = int(secs)
    h, m = divmod(secs // 60, 60)
    return f"{h}h {m}m" if h else f"{m}m"


def fetch_day(garmin, day_str):
    day_data = {}
    errors = {}

    calls = {
        "stats": lambda: garmin.get_stats(day_str),
        "sleep": lambda: garmin.get_sleep_data(day_str),
        "hrv": lambda: garmin.get_hrv_data(day_str),
        "body_battery": lambda: garmin.get_body_battery(day_str, day_str),
        "stress": lambda: garmin.get_stress_data(day_str),
        "steps": lambda: garmin.get_steps_data(day_str),
        "training_readiness": lambda: garmin.get_training_readiness(day_str),
    }

    for key, fn in calls.items():
        result, err = safe_call(key, fn)
        day_data[key] = result
        if err:
            errors[key] = err

    if errors:
        day_data["_errors"] = errors
    return day_data


def render_wellness_markdown(day_str, d):
    stats = d.get("stats") or {}
    sleep = d.get("sleep") or {}
    hrv = d.get("hrv") or {}
    bb = d.get("body_battery") or {}
    stress = d.get("stress") or {}
    tr = d.get("training_readiness") or {}

    lines = [f"# Wellness — {day_str}", ""]

    # Sleep
    dto = get_path(sleep, "dailySleepDTO", default={}) or {}
    total = seconds_to_hm(dto.get("sleepTimeSeconds"))
    deep = seconds_to_hm(dto.get("deepSleepSeconds"))
    light = seconds_to_hm(dto.get("lightSleepSeconds"))
    rem = seconds_to_hm(dto.get("remSleepSeconds"))
    awake = seconds_to_hm(dto.get("awakeSleepSeconds"))
    score = get_path(dto, "sleepScores", "overall", "value")
    quality = get_path(dto, "sleepScores", "overall", "qualifierKey")

    lines.append("## Sleep")
    if total:
        lines.append(f"- Total sleep: {total}")
        if deep:
            lines.append(f"- Deep: {deep}")
        if light:
            lines.append(f"- Light: {light}")
        if rem:
            lines.append(f"- REM: {rem}")
        if awake:
            lines.append(f"- Awake: {awake}")
        if score is not None:
            q = f" ({quality})" if quality else ""
            lines.append(f"- Sleep score: {score}{q}")
    else:
        lines.append("- Not available for this day")
    lines.append("")

    # HRV
    lines.append("## Heart Rate Variability (HRV)")
    last_night = get_path(hrv, "hrvSummary", "lastNightAvg")
    hrv_status = get_path(hrv, "hrvSummary", "status")
    weekly_avg = get_path(hrv, "hrvSummary", "weeklyAvg")
    if last_night is not None:
        lines.append(f"- Last night average: {last_night} ms")
        if hrv_status:
            lines.append(f"- Status: {hrv_status}")
        if weekly_avg is not None:
            lines.append(f"- 7-day average: {weekly_avg} ms")
    else:
        lines.append("- Not available for this day")
    lines.append("")

    # Resting heart rate
    rhr = stats.get("restingHeartRate")
    lines.append("## Resting Heart Rate")
    lines.append(f"- {rhr} bpm" if rhr is not None else "- Not available for this day")
    lines.append("")

    # Body battery
    lines.append("## Body Battery")
    bb_entry = None
    if isinstance(bb, list) and bb:
        bb_entry = bb[0]
    charged = get_path(bb_entry or {}, "charged")
    drained = get_path(bb_entry or {}, "drained")
    if charged is None:
        charged = stats.get("bodyBatteryChargedValue")
    if drained is None:
        drained = stats.get("bodyBatteryDrainedValue")
    highest = stats.get("bodyBatteryHighestValue")
    lowest = stats.get("bodyBatteryLowestValue")
    if any(v is not None for v in (charged, drained, highest, lowest)):
        if charged is not None:
            lines.append(f"- Charged: {charged}")
        if drained is not None:
            lines.append(f"- Drained: {drained}")
        if highest is not None:
            lines.append(f"- Highest: {highest}")
        if lowest is not None:
            lines.append(f"- Lowest: {lowest}")
    else:
        lines.append("- Not available for this day")
    lines.append("")

    # Stress
    lines.append("## Stress")
    avg_stress = stress.get("avgStressLevel", stats.get("averageStressLevel"))
    max_stress = stress.get("maxStressLevel", stats.get("maxStressLevel"))
    if avg_stress is not None and avg_stress != -1:
        lines.append(f"- Average: {avg_stress}")
        if max_stress is not None and max_stress != -1:
            lines.append(f"- Max: {max_stress}")
    else:
        lines.append("- Not available for this day")
    lines.append("")

    # Training readiness
    lines.append("## Training Readiness")
    tr_entry = tr[0] if isinstance(tr, list) and tr else None
    if tr_entry:
        tr_score = tr_entry.get("score")
        tr_level = tr_entry.get("level")
        lines.append(f"- Score: {tr_score} ({tr_level})" if tr_level else f"- Score: {tr_score}")
        feedback = tr_entry.get("feedbackLong") or tr_entry.get("feedbackShort")
        if feedback:
            lines.append(f"- Note: {feedback}")
    else:
        lines.append("- Not available for this day")
    lines.append("")

    # Steps
    lines.append("## Steps")
    total_steps = stats.get("totalSteps")
    lines.append(f"- {total_steps:,} steps" if isinstance(total_steps, int) else "- Not available for this day")
    lines.append("")

    lines.append("_Full raw data for this day is in ../data.json_")
    return "\n".join(lines)


def fmt(value, unit="", digits=1):
    if value is None:
        return None
    try:
        return f"{round(value, digits)}{unit}"
    except Exception:
        return f"{value}{unit}"


def render_activity_markdown(a):
    name = a.get("activityName") or "Workout"
    activity_type = get_path(a, "activityType", "typeKey") or "activity"
    start = a.get("startTimeLocal", "")
    day_str = start.split(" ")[0] if start else "unknown-date"

    duration_s = a.get("duration")
    distance_m = a.get("distance")
    avg_hr = a.get("averageHR")
    max_hr = a.get("maxHR")
    calories = a.get("calories")
    elev_gain = a.get("elevationGain")
    aerobic_te = a.get("aerobicTrainingEffect")
    anaerobic_te = a.get("anaerobicTrainingEffect")
    avg_pace = a.get("averageSpeed")

    lines = [f"# {name}", f"_{activity_type} — {start}_", ""]
    lines.append("## Summary")
    if duration_s is not None:
        lines.append(f"- Duration: {seconds_to_hm(duration_s)}")
    if distance_m is not None:
        lines.append(f"- Distance: {fmt(distance_m / 1000, ' km', 2)}")
    if calories is not None:
        lines.append(f"- Calories: {calories}")
    if elev_gain is not None:
        lines.append(f"- Elevation gain: {fmt(elev_gain, ' m', 0)}")
    lines.append("")

    lines.append("## Heart Rate")
    if avg_hr is not None:
        lines.append(f"- Average: {avg_hr} bpm")
    if max_hr is not None:
        lines.append(f"- Max: {max_hr} bpm")
    if avg_hr is None and max_hr is None:
        lines.append("- Not available")
    lines.append("")

    lines.append("## Training Effect")
    if aerobic_te is not None:
        lines.append(f"- Aerobic: {aerobic_te}")
    if anaerobic_te is not None:
        lines.append(f"- Anaerobic: {anaerobic_te}")
    if aerobic_te is None and anaerobic_te is None:
        lines.append("- Not available")
    lines.append("")

    lines.append("_Full raw data for this activity is in ../data.json_")
    return day_str, "\n".join(lines)


def safe_filename(s):
    keep = "-_. "
    return "".join(c if c.isalnum() or c in keep else "_" for c in s).strip()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=7, help="How many days back to pull")
    parser.add_argument("--activities", type=int, default=20, help="How many recent activities to check")
    args = parser.parse_args()

    WELLNESS_DIR.mkdir(parents=True, exist_ok=True)
    WORKOUTS_DIR.mkdir(parents=True, exist_ok=True)
    store.ensure_dirs()

    log("Logging in with saved token...")
    garmin = login()
    log("Logged in.")

    today = date.today()
    day_list = [(today - timedelta(days=i)).isoformat() for i in range(args.days)]

    failed_days = []
    for day_str in day_list:
        log(f"Pulling wellness data for {day_str}...")
        day_data = fetch_day(garmin, day_str)
        if day_data.get("_errors"):
            failed_days.append(day_str)
        # put_day refuses to overwrite a good day with an empty payload, so a
        # flaky pull degrades to "we keep what we already had".
        store.put_day(day_str, day_data)
        md = render_wellness_markdown(day_str, day_data)
        (WELLNESS_DIR / f"{day_str}.md").write_text(md)

    log("Pulling recent activities...")
    activities, err = safe_call("activities", lambda: garmin.get_activities(0, args.activities))
    if err:
        log(f"  Could not pull activities: {err}")
        activities = []

    cutoff = day_list[-1] if day_list else today.isoformat()
    kept = 0
    for a in activities or []:
        start = a.get("startTimeLocal", "")
        a_day = start.split(" ")[0] if start else ""
        if a_day and a_day < cutoff:
            continue  # older than our window
        store.put_activity(a)
        day_str, md = render_activity_markdown(a)
        activity_id = a.get("activityId", "unknown")
        name_part = safe_filename(a.get("activityName") or "workout")
        filename = f"{day_str}_{name_part}_{activity_id}.md"
        (WORKOUTS_DIR / filename).write_text(md)
        kept += 1

    cov = store.coverage()

    log("")
    log(f"Done. Synced {len(day_list)} day(s) and {kept} activity/activities into the store.")
    if failed_days:
        log(f"Partial data for {len(failed_days)} day(s): {', '.join(failed_days)}")
        log("(Any day that already had good data was left untouched.)")
    log(f"Store now covers {cov['first_day']} -> {cov['last_day']} "
        f"({cov['day_count']} days, {cov['activity_count']} activities).")
    log(f"Wellness notes: {WELLNESS_DIR}")
    log(f"Workout notes:  {WORKOUTS_DIR}")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        traceback.print_exc()
        sys.exit(1)
