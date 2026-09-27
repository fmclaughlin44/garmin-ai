"""
Generates a synthetic dataset so the project can be demonstrated publicly
without publishing anyone's real health data.

Why this exists: the repository is public, but resting heart rate, sleep and
location-tagged workouts are not things to put in public git history. So the
real store is gitignored, and this script produces a fabricated athlete with the
same *shape* of data. Every code path the real dashboard uses runs against it --
including the absent-HRV path, which is kept absent on purpose so the demo shows
the readiness model this project actually ships rather than an idealised one.

The demo athlete is placed mid-block (week 5, the first peak week) because that
is when the dashboard has the most to say: a full 28-day baseline, a workload
ratio in the productive range, and every chart populated.

Everything is seeded, so the demo is byte-identical on every run.

Usage:
  python3 make_demo_data.py                 # writes web/demo-data.json
  python3 build_dashboard.py --data web/demo-data.json --out demo/index.html
"""

import json
import math
import random
from datetime import date, timedelta
from pathlib import Path

import coach
import signals

BASE = Path(__file__).resolve().parent
OUT = BASE / "web" / "demo-data.json"
PLAN_JSON = BASE / "plan" / "plan.json"

AS_OF = date(2026, 10, 21)   # Wednesday of week 5 -- the first peak week
HISTORY_DAYS = 70
SEED = 20261115

RNG = random.Random(SEED)

# Baseline physiology for the fictional athlete.
RHR_BASE = 48
SLEEP_BASE = 7.7
HR_MAX = 187


def jitter(spread):
    return RNG.uniform(-spread, spread)


def session_for(weekday, week_miles):
    """A week's training pattern, mirroring the plan's own template.

    Returns (type_key, minutes, miles, avg_hr) or None for a rest day.
    Long run lands on Sunday, quality on Tuesday, medium-long on Thursday.
    """
    long_miles = round(week_miles * 0.42, 1)
    if weekday == 0:      # Mon -- recovery cross-train
        return ("lap_swimming", 32, None, 132)
    if weekday == 1:      # Tue -- quality
        miles = round(week_miles * 0.21, 1)
        return ("running", int(miles * 8.4), miles, 163)
    if weekday == 2:      # Wed -- easy
        miles = round(week_miles * 0.16, 1)
        return ("running", int(miles * 9.8), miles, 145)
    if weekday == 3:      # Thu -- medium-long with marathon pace
        miles = round(week_miles * 0.25, 1)
        return ("running", int(miles * 9.1), miles, 156)
    if weekday == 4:      # Fri -- rest or spin
        return ("cycling", 45, None, 121) if RNG.random() < 0.55 else None
    if weekday == 5:      # Sat -- shakeout
        miles = round(week_miles * 0.10, 1)
        return ("running", int(miles * 10.0), miles, 141)
    return ("running", int(long_miles * 9.5), long_miles, 152)   # Sun -- long run


def week_miles_for(day: date, plan_days: dict) -> float:
    """Take the week's volume target from the real plan where it covers the day."""
    entry = plan_days.get(day.isoformat())
    if entry and entry.get("week_miles_target"):
        return float(entry["week_miles_target"])
    return 26.0   # before the block starts, a modest base week


def build_dataset():
    plan = json.loads(PLAN_JSON.read_text())
    plan_days = plan.get("days", {})

    days, activities = {}, []
    activity_id = 900000

    for offset in range(HISTORY_DAYS - 1, -1, -1):
        day = AS_OF - timedelta(days=offset)
        iso = day.isoformat()
        miles_target = week_miles_for(day, plan_days)
        session = session_for(day.weekday(), miles_target)

        # --- the session ---
        hard_yesterday = False
        if session:
            type_key, minutes, miles, avg_hr = session
            avg_hr = int(avg_hr + jitter(4))
            payload = {
                "activityId": activity_id,
                "startTimeLocal": f"{iso} 07:12:00",
                "activityType": {"typeKey": type_key},
                "activityName": {"running": "Morning Run", "cycling": "Ride",
                                 "lap_swimming": "Pool Swim"}[type_key],
                "duration": minutes * 60,
                "averageHR": avg_hr,
                "maxHR": int(avg_hr + RNG.uniform(12, 24)),
                "calories": int(minutes * 11.5),
            }
            if miles:
                payload["distance"] = miles * signals.METERS_PER_MILE
            activities.append(payload)
            activity_id += 1
            hard_yesterday = avg_hr > 158

        # --- overnight recovery, responding to the previous day's work ---
        prev = AS_OF - timedelta(days=offset + 1)
        prev_hard = False
        for a in activities:
            if a["startTimeLocal"].startswith(prev.isoformat()) and a["averageHR"] > 158:
                prev_hard = True

        rhr = RHR_BASE + (2.2 if prev_hard else 0) + jitter(1.6)
        # a mild illness blip, so the demo shows the baseline doing its job
        if 24 <= offset <= 26:
            rhr += 5.5
        sleep_hours = SLEEP_BASE - (0.35 if hard_yesterday else 0) + jitter(0.55)
        stress = 24 + (6 if prev_hard else 0) + jitter(5)
        bb_wake = 82 - (9 if prev_hard else 0) + jitter(6)

        sleep_seconds = int(sleep_hours * 3600)
        days[iso] = {
            "stats": {
                "restingHeartRate": int(round(rhr)),
                "averageStressLevel": int(round(max(12, stress))),
                "maxStressLevel": int(round(max(40, stress * 2.4))),
                "bodyBatteryAtWakeTime": int(round(min(98, max(30, bb_wake)))),
                "bodyBatteryHighestValue": int(round(min(100, max(35, bb_wake + 4)))),
                "bodyBatteryLowestValue": int(round(max(8, bb_wake - 55))),
                "totalSteps": int(9000 + jitter(2600) + (3200 if session else 0)),
                "avgWakingRespirationValue": round(13.6 + jitter(0.9), 1),
            },
            "sleep": {"dailySleepDTO": {
                "sleepTimeSeconds": sleep_seconds,
                "deepSleepSeconds": int(sleep_seconds * (0.19 + jitter(0.02))),
                "remSleepSeconds": int(sleep_seconds * (0.21 + jitter(0.02))),
                "lightSleepSeconds": int(sleep_seconds * 0.52),
                "awakeSleepSeconds": int(sleep_seconds * 0.06),
            }},
            # Left empty on purpose: the real device reports neither, and the
            # demo should show the model this project actually ships.
            "hrv": {},
            "training_readiness": [],
        }

    return days, activities, plan


def main() -> None:
    days, activities, plan = build_dataset()

    data = signals.build(as_of=AS_OF, days=days, activities=activities, plan=plan,
                         config={"weight_kg": 72, "sleep_target_hours": 7.5,
                                 "hr_max": HR_MAX,
                                 "readiness_weights": {"resting_hr": 30, "sleep": 30,
                                                        "body_battery": 15, "stress": 10,
                                                        "training_load": 15}})
    data["coach"] = coach.build_rules_note(data)
    data["demo"] = True
    data["meta"]["demo_note"] = (
        f"Synthetic dataset generated by make_demo_data.py (seed {SEED}). "
        "Shaped like real Garmin output, but fabricated — no real health data here."
    )

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(data, indent=2, default=str))

    print(f"Demo data written to {OUT}")
    print(f"  as of {AS_OF} · {len(days)} days, {len(activities)} activities")
    print(f"  readiness {data['readiness']['score']} ({data['readiness']['band']})")
    print(f"  workload ratio {data['load']['ratio']} [{data['load']['zone']}]")
    print(f"  projection {data['projection']['predicted']} vs goal {data['projection']['goal']}")
    print(f"  adherence {data['adherence']['key_sessions_done_to_date']}/"
          f"{data['adherence']['key_sessions_due_to_date']}")


if __name__ == "__main__":
    main()
