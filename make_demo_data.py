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

import adapt
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
    """A week's training pattern, mirroring the real plan's own shape.

    The plan runs quality on Wednesday and the long run on Saturday, with Sunday
    as cross-training and Friday optional. The demo follows the same week so its
    activities line up with the plan days the dashboard matches them against.

    Returns (type_key, minutes, miles, avg_hr) or None for a rest day.
    """
    long_miles = round(week_miles * 0.38, 1)
    if weekday == 0:      # Mon -- easy + Upper A
        miles = round(week_miles * 0.14, 1)
        return ("running", int(miles * 9.4), miles, 143)
    if weekday == 1:      # Tue -- easy + Lower A
        miles = round(week_miles * 0.17, 1)
        return ("running", int(miles * 9.4), miles, 145)
    if weekday == 2:      # Wed -- quality
        miles = round(week_miles * 0.18, 1)
        return ("running", int(miles * 8.5), miles, 164)
    if weekday == 3:      # Thu -- easy + Upper B
        miles = round(week_miles * 0.14, 1)
        return ("running", int(miles * 9.4), miles, 144)
    if weekday == 4:      # Fri -- optional pressure-valve day
        return ("cycling", 42, None, 122) if RNG.random() < 0.35 else None
    if weekday == 5:      # Sat -- the long run
        return ("running", int(long_miles * 9.3), long_miles, 151)
    # Sun -- easy cross-train, Z2
    return ("lap_swimming", 34, None, 131) if RNG.random() < 0.7 else None


def week_miles_for(day: date, plan_days: dict) -> float:
    """Take the week's volume target from the real plan where it covers the day."""
    entry = plan_days.get(day.isoformat())
    if entry and entry.get("week_miles_target"):
        return float(entry["week_miles_target"])
    return 34.0   # before the block starts: an established base, not a standing start


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

        # A synthetic overnight heart-rate series, so the nocturnal-drop signal
        # has something to read. Shaped like the real thing: starts near waking
        # rate, falls to a minimum partway through, drifts back up before waking.
        # A hard previous day gives a shallower drop that bottoms out later,
        # which is the pattern the real data shows.
        onset = rhr + (7 if prev_hard else 11) + jitter(1.5)
        trough = rhr - 1 + jitter(1.0)
        trough_at = (0.72 if prev_hard else 0.42) + jitter(0.08)
        samples = max(30, int(sleep_seconds / 120))
        series = []
        for s in range(samples):
            position = s / (samples - 1)
            if position <= trough_at:
                value = onset + (trough - onset) * (position / max(trough_at, 0.05))
            else:
                rise = (position - trough_at) / max(1 - trough_at, 0.05)
                value = trough + (onset - trough) * 0.55 * rise
            series.append({"value": int(round(value + jitter(1.2))),
                           "startGMT": s * 120000})

        days[iso] = {
            "stats": {
                "restingHeartRate": int(round(rhr)),
                "averageStressLevel": int(round(max(12, stress))),
                "maxStressLevel": int(round(max(40, stress * 2.4))),
                "bodyBatteryAtWakeTime": int(round(min(98, max(30, bb_wake)))),
                "bodyBatteryHighestValue": int(round(min(100, max(35, bb_wake + 4)))),
                "bodyBatteryLowestValue": int(round(max(8, bb_wake - 55))),
                "totalSteps": int(9000 + jitter(2600) + (3200 if session else 0)),
                # Calorie fields, so the nutrition panel has a measured
                # maintenance figure to work from like the real build does.
                "bmrKilocalories": 1980,
                "activeKilocalories": int((session[1] * 7.4 if session else 0) + jitter(90) + 260),
                "totalKilocalories": int(1980 + (session[1] * 7.4 if session else 0) + jitter(90) + 260),
                "avgWakingRespirationValue": round(13.6 + jitter(0.9), 1),
            },
            "sleep": {
                "dailySleepDTO": {
                    "sleepTimeSeconds": sleep_seconds,
                    "deepSleepSeconds": int(sleep_seconds * (0.19 + jitter(0.02))),
                    "remSleepSeconds": int(sleep_seconds * (0.21 + jitter(0.02))),
                    "lightSleepSeconds": int(sleep_seconds * 0.52),
                    "awakeSleepSeconds": int(sleep_seconds * 0.06),
                },
                "sleepHeartRate": series,
            },
            # Left empty on purpose: the real device reports neither, and the
            # demo should show the model this project actually ships.
            "hrv": {},
            "training_readiness": [],
        }

    # Fabricated Garmin snapshots. These must be injected rather than read from
    # the store: the real ones are this athlete's actual VO2 max and race
    # predictions, and the demo is published.
    snapshots = {"max_metrics": {}, "race_predictions": {}}
    for weeks_back in range(9):
        observed = (AS_OF - timedelta(weeks=weeks_back)).isoformat()
        snapshots["max_metrics"][observed] = [{
            "generic": {"calendarDate": observed,
                        "vo2MaxPreciseValue": round(52.0 + (8 - weeks_back) * 0.22, 1),
                        "vo2MaxValue": round(52.0 + (8 - weeks_back) * 0.22)}
        }]
    snapshots["race_predictions"][AS_OF.isoformat()] = {
        "calendarDate": AS_OF.isoformat(),
        "time5K": 1205, "time10K": 2530, "timeHalfMarathon": 5820, "timeMarathon": 13620,
    }

    # A fabricated 70.3 before the block, so the Ironman section has a reference
    # race to extrapolate from the way the real build does.
    race_day = (AS_OF - timedelta(days=63)).isoformat()
    for leg_id, (type_key, name, minutes, miles, hr) in enumerate([
        ("open_water_swimming", "70.3 Swim", 37, 1.2, 152),
        ("cycling", "70.3 Bike", 178, 56.0, 150),
        ("running", "70.3 Run", 112, 13.1, 164),
    ]):
        activities.append({
            "activityId": 800000 + leg_id,
            "startTimeLocal": f"{race_day} 07:0{leg_id}:00",
            "activityType": {"typeKey": type_key},
            "activityName": name,
            "duration": minutes * 60,
            "distance": miles * signals.METERS_PER_MILE,
            "averageHR": hr,
            "maxHR": hr + 14,
        })

    # Hand-logged values the fictional athlete recorded: bodyweight, morning
    # soreness and energy, and RPE after sessions.
    manual = {}
    for offset in range(0, HISTORY_DAYS, 1):
        day = AS_OF - timedelta(days=offset)
        iso = day.isoformat()
        entry = {}
        if offset % 7 == 0:
            entry["weight_lb"] = round(168 - offset * 0.02 + jitter(0.6), 1)
        if offset < 30:
            hard = day.weekday() in (2, 5)
            entry["soreness"] = int(max(1, min(10, (5 if hard else 3) + jitter(1.4))))
            entry["energy"] = int(max(1, min(10, (6 if hard else 7) + jitter(1.2))))
            if day.weekday() in (0, 1, 2, 3, 5):
                entry["rpe"] = int(max(1, min(10, (8 if hard else 4) + jitter(1.0))))
        if entry:
            entry["source"] = "manual"
            manual[iso] = entry

    return days, activities, plan, snapshots, race_day, manual


def main() -> None:
    days, activities, plan, snapshots, race_day, manual = build_dataset()

    # Start from the real config so the demo can't drift away from it, and
    # override only the things that are specific to the fictional athlete.
    config = json.loads((BASE / "config.json").read_text())
    config.update({
        "weight_lb": 168,
        "hr_max": HR_MAX,
        "reference_race": {"name": "70.3 (demo)", "date": race_day, "distance": "70.3"},
        "ironman_2027": {"name": "Ironman (140.6)", "date": "2027-09-12",
                         "goal_time": "13:00:00", "swim_miles": 2.4, "bike_miles": 112.0,
                         "run_miles": signals.MARATHON_MILES, "transition_minutes": 15},
    })

    data = signals.build(as_of=AS_OF, days=days, activities=activities, plan=plan,
                         snapshots=snapshots, manual=manual, config=config)
    data = adapt.annotate(data)
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
    print(f"  form {data['form']['fitness']}/{data['form']['fatigue']} = "
          f"{data['form']['form']} [{data['form']['zone']}]")
    print(f"  nocturnal drop {data['recovery']['nocturnal']['today']} "
          f"vs baseline {data['recovery']['nocturnal']['baseline']}")
    print(f"  Garmin (synthetic) says {data['garmin']['race_prediction']['marathon']}, "
          f"VO2 {data['garmin']['vo2_max']}")
    n = data["nutrition"]
    print(f"  nutrition: {n['target_kcal']}kcal C{n['carbs_g']} P{n['protein_g']} F{n['fat_g']} [{n['band']}]")
    pl = data.get("perceived_load", {})
    if pl.get("available"):
        print(f"  perceived load: {pl['paired_sessions']} paired sessions, "
              f"drift {pl.get('drift')} [{pl.get('band')}]")
    print(f"  adaptation: {data['adaptation']['level']} — "
          f"{len(data['adaptation']['triggers'])} trigger(s)")
    im = data["triathlon"]["ironman"]
    if im.get("available"):
        print(f"  ironman 140.6: {im['fast']} – {im['slow']} (mid {im['midpoint']})")


if __name__ == "__main__":
    main()
