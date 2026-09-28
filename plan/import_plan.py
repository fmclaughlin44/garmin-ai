"""
Converts plan/marathon_plan.json into plan/plan.json, the schema the pipeline reads.

marathon_plan.json is the source of truth: it is the plan Frank actually follows,
written the way a coach writes one -- prose titles, mileage ranges, phase labels.
This script does not invent or alter any of it. It reads that file and derives
the machine-readable fields the rest of the pipeline needs:

    kind    rest | cross | easy | quality | long | race
    miles   planned run mileage (a range becomes its midpoint)
    key     whether the block depends on this session
    lift    which lifting template, if any

Nothing downstream ever edits the base plan, so re-running this is the only way
plan.json changes. Every derived number is traceable back to a line in
marathon_plan.json; where the source is a range or a duration, the assumption is
stated in the function that makes it.

Usage:
  python3 plan/import_plan.py
"""

import json
import re
from datetime import date, datetime, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
SOURCE = HERE / "marathon_plan.json"
OUT = HERE / "plan.json"

MARATHON_MILES = 26.2188

# Pace used to turn a duration into a distance. Easy running sits at 9:00-9:45
# in this plan, so 9:30 is the honest middle for "easy 30-40min".
EASY_MIN_PER_MILE = 9.5

LIFT_KEYS = [
    (r"upper\s*a\b|upper\s*\(?push", "upper_push"),
    (r"upper\s*b\b|upper\s*\(?pull", "upper_pull"),
    (r"light upper|full body", "upper_full"),
    (r"lower\s*a\b", "lower_a"),
    (r"lower\s*b\b", "lower_b"),
]


def midpoint(low: str, high: str = None) -> float:
    if high is None:
        return float(low)
    return round((float(low) + float(high)) / 2, 1)


def parse_miles(text: str) -> float:
    """Pull planned running mileage out of a session title.

    Every distance pattern below guards "mi" with ``(?!n)``. Without it "Easy
    30-40min" parses as a 35-mile run, which put week 1 at 96 miles.

    Handles the four shapes this plan uses:
      "Long Run 16mi"            -> 16
      "Easy Run 4-5mi"           -> 4.5   (a range becomes its midpoint)
      "Easy 30-40min + strides"  -> 3.7   (duration at easy pace)
      "MP 4x1.5mi @ 8:34"        -> 6     (reps x distance) plus warm-up/cool-down
    """
    lowered = text.lower()

    if "marathon" in lowered and "race day" in lowered:
        return MARATHON_MILES

    # Interval work: reps x distance, plus a conventional warm-up and cool-down.
    reps = re.search(r"(\d+)\s*x\s*([\d.]+)\s*mi(?!n)", lowered)
    if reps:
        work = int(reps.group(1)) * float(reps.group(2))
        return round(work + 3, 1)          # ~2mi warm-up, ~1mi cool-down

    # An explicit warm-up/cool-down spec, e.g. "2WU/3/1CD".
    spec = re.search(r"(\d+)\s*wu\s*/\s*([\d.]+)\s*/\s*(\d+)\s*cd", lowered)
    if spec:
        return round(sum(float(g) for g in spec.groups()), 1)

    # A plain distance or distance range.
    rng = re.search(r"(\d+(?:\.\d+)?)\s*-\s*(\d+(?:\.\d+)?)\s*mi(?!n)", lowered)
    if rng:
        base = midpoint(rng.group(1), rng.group(2))
        return round(base + 3, 1) if _is_quality(lowered) else base
    single = re.search(r"(\d+(?:\.\d+)?)\s*mi(?!n)", lowered)
    if single:
        base = float(single.group(1))
        return round(base + 3, 1) if _is_quality(lowered) else base

    # A duration instead of a distance.
    duration = re.search(r"(\d+)\s*-\s*(\d+)\s*min", lowered)
    if duration:
        minutes = midpoint(duration.group(1), duration.group(2))
    else:
        one = re.search(r"(\d+)\s*min", lowered)
        minutes = float(one.group(1)) if one else 0
    if minutes and not re.search(r"cross|bike|swim|rest", lowered):
        return round(minutes / EASY_MIN_PER_MILE, 1)
    return 0.0


def _is_quality(text: str) -> bool:
    return bool(re.search(r"tempo|cruise|\bmp\b|short sharp|tune-up", text))


def classify(day: dict) -> str:
    """Session kind, from the title and the plan's own discipline tag."""
    title = day["title"].lower()
    disc = (day.get("disc") or "").lower()

    if "marathon" in title and "race day" in title:
        return "race"
    if "long run" in title:
        return "long"
    if _is_quality(title):
        return "quality"
    if disc == "swim" or re.search(r"cross-?train|bike|swim", title):
        # An optional day offering a run OR a cross-train is a rest day for
        # planning purposes; anything done on it is a bonus, not a requirement.
        return "rest" if "optional" in title else "cross"
    if "rest" in title or "optional" in title:
        return "rest"
    if re.search(r"easy|shakeout|strides", title):
        return "easy"
    return "easy"


def find_lift(day: dict) -> str:
    title = day["title"].lower()
    for pattern, key in LIFT_KEYS:
        if re.search(pattern, title):
            return key
    return None


def build() -> dict:
    source = json.loads(SOURCE.read_text())
    meta = source["meta"]
    start = datetime.fromisoformat(meta["start"]).date()

    days, weeks_meta = {}, []
    for index, week in enumerate(source["plan"]):
        week_start = start + timedelta(weeks=index)
        week_miles = 0.0

        for offset, day in enumerate(week["days"]):
            iso = (week_start + timedelta(days=offset)).isoformat()
            kind = classify(day)
            # An optional or rest day contributes nothing to the week's target.
            # Anything run on one counts as a bonus when matched, not as a plan.
            miles = 0.0 if kind in ("rest", "cross") else parse_miles(day["title"])
            lift = find_lift(day)
            # The plan's own rule: "Never cut the long run or Wednesday quality
            # to save a lift." Those, and race day, are the key sessions.
            key = kind in ("long", "quality", "race")
            week_miles += miles

            days[iso] = {
                "session": day["title"],
                "target": day.get("detail") or "",
                "kind": kind,
                "miles": miles,
                "key": key,
                "lift": lift,
                "discipline": day.get("disc"),
                "phase": f"Week {week['wk']} — {week['label']}",
                "week": week["wk"],
            }

        week_miles = round(week_miles, 1)
        for offset in range(len(week["days"])):
            iso = (week_start + timedelta(days=offset)).isoformat()
            if iso in days:
                days[iso]["week_miles_target"] = week_miles

        weeks_meta.append({
            "week": week["wk"],
            "phase": f"Week {week['wk']} — {week['label']}",
            "headline": f"{week['phase'].title()} · long run {week['longRun']}",
            "miles_target": week_miles,
            "start": week_start.isoformat(),
            "end": (week_start + timedelta(days=6)).isoformat(),
            "long_run": week["longRun"],
            "phase_key": week["phase"],
        })

    goal = meta["goal"]
    hours, minutes = goal["primary"].replace("sub-", "").split(":")
    goal_seconds = int(hours) * 3600 + int(minutes) * 60

    source_paces = meta["paces"]
    return {
        "race": {
            "name": "Marathon",
            "date": meta["raceDate"],
            "distance": "Marathon (26.2mi)",
            "goal": f"Sub-{goal['primary'].replace('sub-', '')} ({goal['primaryPace']}/mi)",
            "goal_seconds": goal_seconds,
            "floor": f"Sub-{goal['floor'].replace('sub-', '')} ({goal['floorPace']}/mi)",
            "start_time": "Morning start",
        },
        "block": {
            "start": meta["start"],
            "end": (start + timedelta(weeks=meta["weeks"], days=-1)).isoformat(),
            "weeks": meta["weeks"],
            "structure": meta["structure"],
            "context": meta["block"],
        },
        "paces": {
            "marathon": source_paces["marathonPace"],
            "easy": source_paces["easy"],
            "long": source_paces["easy"],
            "tempo": source_paces["tempo"],
            "threshold": source_paces["tempo"],
            "cruise": source_paces["cruise"],
            "sub_four": source_paces["subFourMP"],
        },
        "athlete": meta["athleteContext"],
        "rules": meta["rules"],
        "lifting": source["lifting"],
        "weeks": weeks_meta,
        "key_session_keywords": ["long run", "tempo", "mp ", "cruise", "race day", "short sharp"],
        "days": days,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "generated_by": "plan/import_plan.py from plan/marathon_plan.json",
    }


if __name__ == "__main__":
    plan = build()
    OUT.write_text(json.dumps(plan, indent=2))
    print(f"Wrote {OUT} from {SOURCE.name}")
    print(f"  {len(plan['days'])} days · {plan['block']['weeks']} weeks · "
          f"goal {plan['race']['goal']}")
    print("  weekly volume: " + " → ".join(str(w["miles_target"]) for w in plan["weeks"]) + " mi")
    for w in plan["weeks"]:
        print(f"    W{w['week']} {w['phase_key']:8s} long {w['long_run']}")
