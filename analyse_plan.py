"""
Audits the training plan itself: intensity distribution, ramp rate, and the
structural patterns that predict injury.

The plan importer answers "what am I doing today". This answers a different
question: "is this plan any good". It reads plan/marathon_plan.json and checks it
against the conventions that actually have evidence behind them —

  80/20            roughly 80% of weekly volume genuinely easy, 20% at or above
                   threshold. The most robust finding in endurance training, and
                   the one most often violated by self-written plans, which drift
                   toward too much moderate-hard work.
  long run share   a long run beyond ~30-35% of weekly volume is the classic
                   overuse setup, especially when weekly volume is modest.
  ramp rate        week-to-week volume increases beyond ~10% raise injury risk.
  hard spacing     hard sessions need 48h between them for the adaptation to
                   land rather than accumulate.
  peak volume      a time goal implies a volume range; being far under it means
                   the goal is being asked of fitness that isn't being built.

Nothing here is part of the pipeline. It is an analysis of the plan, run when the
plan changes.

Usage:
  python3 analyse_plan.py
"""

import json
import re
from datetime import datetime, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
SOURCE = HERE / "plan" / "marathon_plan.json"

# Volume ranges typically associated with marathon time goals for a
# single-sport-trained runner. Wide on purpose: these are rules of thumb, not
# laws, and a triathlon base shifts them down because the aerobic engine is
# already built.
GOAL_VOLUME = {
    "3:00": (55, 80),
    "3:15": (45, 70),
    "3:30": (40, 60),
    "3:45": (35, 55),
    "4:00": (28, 45),
}

EASY_PACE_MIN = 9.0   # from the plan's own paces block


def quality_miles(day: dict, long_run_text: str) -> dict:
    """Miles at marathon pace and miles at threshold or faster, separately.

    Splitting the two matters more than it looks. The 80/20 convention is about
    threshold and above -- the work that needs real recovery. Marathon pace sits
    between easy and threshold: it is specific, it is the whole point of a
    marathon block, and lumping it in with intervals makes a well-balanced plan
    read as dangerously hard.

    Warm-ups, cool-downs, recovery jogs and the easy portion of a long run all
    count as easy, because they are.
    """
    title = (day.get("title") or "").lower()
    detail = (day.get("detail") or "").lower()
    text = f"{title} {detail}"

    mp, threshold = 0.0, 0.0

    # Race day is neither: it is the thing the block is for.
    if "marathon" in title and "race day" in title:
        return {"mp": 0.0, "threshold": 0.0, "race": 26.2188}

    # A tune-up race is threshold-and-faster work, and a hard day for spacing.
    if "5k race" in title:
        return {"mp": 0.0, "threshold": 3.1, "race": 0.0}

    # Reps given in metres.
    metric = re.search(r"(\d+)\s*x\s*(\d+)\s*m\b", text)
    if metric:
        work = int(metric.group(1)) * int(metric.group(2)) / 1609.344
        return {"mp": 0.0, "threshold": round(work, 1), "race": 0.0}

    # Interval or marathon-pace reps: reps x distance.
    reps = re.search(r"(\d+)\s*x\s*([\d.]+)\s*mi(?!n)", text)
    if reps:
        work = int(reps.group(1)) * float(reps.group(2))
        # "@ 8:34" is marathon pace; a tempo or cruise pace is faster.
        if "mp" in title or meta_marathon_pace() in text:
            mp += work
        else:
            threshold += work
        return {"mp": mp, "threshold": threshold, "race": 0.0}

    # A continuous tempo or cruise effort.
    tempo = re.search(r"(?:tempo|cruise)\s*([\d.]+)\s*mi(?!n)", title)
    if tempo:
        return {"mp": 0.0, "threshold": float(tempo.group(1)), "race": 0.0}

    # A marathon-pace tune-up or sharpener.
    sharpener = re.search(r"(?:mp tune-up|short sharp:)\s*([\d.]+)\s*mi(?!n)", title)
    if sharpener:
        return {"mp": float(sharpener.group(1)), "threshold": 0.0, "race": 0.0}

    # A long run with a marathon-pace portion. Matched on the TITLE only: the
    # detail field routinely mentions "long runs" in passing ("leg volume drops
    # to protect long runs"), and matching that credited an easy Tuesday with
    # the Saturday long run's marathon-pace miles.
    if "long run" in title:
        segment = re.search(r"(?:last|middle|first)\s+([\d.]+)\s*(?:mi)?\s*@", detail)
        if not segment:
            segment = re.search(r"(?:last|middle|first)\s+([\d.]+)\s*(?:mi)?\s*@",
                                long_run_text.lower())
        if segment:
            return {"mp": float(segment.group(1)), "threshold": 0.0, "race": 0.0}

    return {"mp": 0.0, "threshold": 0.0, "race": 0.0}


_MARATHON_PACE = None


def meta_marathon_pace() -> str:
    global _MARATHON_PACE
    if _MARATHON_PACE is None:
        _MARATHON_PACE = json.loads(SOURCE.read_text())["meta"]["goal"]["primaryPace"]
    return _MARATHON_PACE


def total_miles(day: dict) -> float:
    """Total session mileage, mirroring the importer's parsing."""
    import sys
    sys.path.insert(0, str(HERE / "plan"))
    import import_plan
    kind = import_plan.classify(day)
    if kind in ("rest", "cross"):
        return 0.0
    return import_plan.parse_miles(day["title"])


def main():
    plan = json.loads(SOURCE.read_text())
    meta = plan["meta"]
    goal = meta["goal"]["primary"].replace("sub-", "")
    start = datetime.fromisoformat(meta["start"]).date()

    print(f"Plan audit — {meta['block']}")
    print(f"Goal {meta['goal']['primary']} ({meta['goal']['primaryPace']}/mi), "
          f"{meta['weeks']} weeks to {meta['raceDate']}\n")

    rows = []
    for index, week in enumerate(plan["plan"]):
        total = sum(total_miles(d) for d in week["days"])
        mp = threshold = race = 0.0
        hard_days = []
        for i, day in enumerate(week["days"]):
            q = quality_miles(day, week.get("longRun", ""))
            mp += q["mp"]
            threshold += q["threshold"]
            race += q["race"]
            # Only threshold-and-above counts as a hard day for spacing: marathon
            # pace inside a long run does not need 48 hours either side.
            if q["threshold"] > 0:
                hard_days.append(i)

        training = total - race          # race day isn't training volume
        long_run = max((total_miles(d) for d in week["days"]
                        if "long run" in d["title"].lower()), default=0)

        rows.append({
            "wk": week["wk"], "phase": week["phase"], "total": round(total, 1),
            "training": round(training, 1), "mp": round(mp, 1),
            "threshold": round(threshold, 1), "race": race,
            "easy": round(training - mp - threshold, 1),
            "long_run": long_run,
            "long_pct": round(100 * long_run / training, 1) if training else 0,
            "hard_days": hard_days,
            "back_to_back": any(b - a == 1 for a, b in zip(hard_days, hard_days[1:])),
        })

    print(f"{'Wk':<4}{'Phase':<10}{'Vol':>6}{'Easy':>7}{'MP':>6}{'Thr':>6}"
          f"{'Easy%':>7}{'Long':>6}{'Long%':>7}{'vsPk':>7}  Flags")
    print("-" * 86)
    for i, r in enumerate(rows):
        easy_pct = 100 * r["easy"] / r["training"] if r["training"] else 0
        # Measured against the highest week so far, not the week immediately
        # before. A planned dip (a race week, a cutback) otherwise makes the
        # return to normal volume read as a huge spike, which it isn't.
        ramp = ""
        change = None
        if i:
            reference = max(x["training"] for x in rows[:i])
            if reference:
                change = 100 * (r["training"] - reference) / reference
                ramp = f"{change:+.0f}%"

        flags = []
        if r["training"] and easy_pct < 75:
            flags.append("under 75% easy")
        if r["long_pct"] > 35:
            flags.append(f"long run {r['long_pct']:.0f}% of week")
        if r["back_to_back"]:
            flags.append("hard days adjacent")
        if change is not None and change > 12 and r["wk"] != 2:
            flags.append("ramp >12% over previous peak")

        label = "RACE" if r["race"] else f"{r['training']:.1f}"
        print(f"{r['wk']:<4}{r['phase']:<10}{label:>6}{r['easy']:>7.1f}{r['mp']:>6.1f}"
              f"{r['threshold']:>6.1f}{easy_pct:>6.0f}%{r['long_run']:>6.1f}"
              f"{r['long_pct']:>6.0f}%{ramp:>7}  {', '.join(flags)}")

    # Block level, excluding race week: you don't train in race week, and the
    # marathon itself isn't a stimulus to be balanced against anything.
    body = [r for r in rows if not r["race"]]
    volume = sum(r["training"] for r in body)
    easy = sum(r["easy"] for r in body)
    mp = sum(r["mp"] for r in body)
    threshold = sum(r["threshold"] for r in body)
    peak = max(r["training"] for r in body)

    print(f"\nBlock totals (race week excluded)")
    print(f"  volume            {volume:.0f} mi over {len(body)} weeks "
          f"({volume / len(body):.1f}/wk average), peak {peak:.1f}")
    print(f"  intensity         {100 * easy / volume:.0f}% easy · "
          f"{100 * mp / volume:.0f}% marathon pace · "
          f"{100 * threshold / volume:.0f}% threshold or faster")
    print(f"  80/20 check       {100 * threshold / volume:.0f}% at threshold+ "
          f"(the convention is ~20%, counting threshold and above)")
    low, high = GOAL_VOLUME.get(goal, (None, None))
    if low:
        verdict = "inside" if low <= peak <= high else ("below" if peak < low else "above")
        print(f"  volume for sub-{goal} conventional range {low}-{high} mi/wk at peak — "
              f"{verdict} it")
    print(f"  long run share    {min(r['long_pct'] for r in body if r['long_run']):.0f}%"
          f"-{max(r['long_pct'] for r in body):.0f}% of weekly volume "
          f"(the convention is under ~35%)")
    adjacent = [r["wk"] for r in body if r["back_to_back"]]
    print(f"  hard days adjacent {'weeks ' + ', '.join(map(str, adjacent)) if adjacent else 'none'}")
    print(f"  threshold sessions {sum(1 for r in body for _ in r['hard_days'])} in "
          f"{len(body)} weeks")

    print("\nConventions with evidence behind them, not laws. They also assume a")
    print("runner built by running: a triathlon base shifts the volume ranges down,")
    print("because the aerobic engine is already there.")


if __name__ == "__main__":
    main()
