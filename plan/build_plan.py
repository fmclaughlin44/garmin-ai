"""
Generates plan/plan.json -- the 8-week marathon block.

The plan is generated from a spec rather than hand-written so that the derived
numbers stay consistent: every training pace below is computed from the single
goal time, and the day-by-day calendar is expanded from a week template. Change
GOAL_TIME and the whole plan re-derives.

Block: Mon Sep 21 2026 -> Sun Nov 15 2026 (8 weeks), race on the final Sunday.
Context: this follows IRONMAN 70.3 Wisconsin (Sep 12 2026), so week 1 is a
deliberate re-entry week rather than a training week -- the aerobic base is
already banked, what's missing is run-specific volume and marathon pace.

Usage:
  python3 plan/build_plan.py
"""

import json
from datetime import date, datetime, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
OUT = HERE / "plan.json"

# --------------------------------------------------------------------------
# The single source of truth
# --------------------------------------------------------------------------

RACE_NAME = "Marathon — goal race"
RACE_DATE = date(2026, 11, 15)          # Sunday
BLOCK_START = date(2026, 9, 21)         # Monday of week 1
GOAL_TIME = "3:45:00"
MARATHON_MILES = 26.2188


def parse_goal_seconds(hhmmss: str) -> int:
    h, m, s = (int(x) for x in hhmmss.split(":"))
    return h * 3600 + m * 60 + s


def fmt_pace(seconds_per_mile: float) -> str:
    total = int(round(seconds_per_mile))
    return f"{total // 60}:{total % 60:02d}"


def derive_paces(goal_seconds: int) -> dict:
    """Training paces as offsets from goal marathon pace.

    Offsets follow the conventional ranges (Daniels/Pfitzinger territory):
    easy runs sit well above race pace to keep them genuinely aerobic, threshold
    sits ~30s below, and intervals ~50s below.
    """
    mp = goal_seconds / MARATHON_MILES
    return {
        "marathon": fmt_pace(mp),
        "marathon_seconds": round(mp),
        "easy": f"{fmt_pace(mp + 75)}–{fmt_pace(mp + 105)}",
        "long": f"{fmt_pace(mp + 45)}–{fmt_pace(mp + 75)}",
        "threshold": f"{fmt_pace(mp - 35)}–{fmt_pace(mp - 25)}",
        "interval": f"{fmt_pace(mp - 65)}–{fmt_pace(mp - 50)}",
        "half": fmt_pace(mp - 25),
    }


PACES = derive_paces(parse_goal_seconds(GOAL_TIME))
MP = PACES["marathon"]
EASY = PACES["easy"]
LONG = PACES["long"]
THR = PACES["threshold"]
INT = PACES["interval"]

# --------------------------------------------------------------------------
# The block, week by week
# --------------------------------------------------------------------------
# Each week: (phase label, headline, target weekly miles, {weekday: (session, target)})
# Weekday keys are 0=Mon .. 6=Sun. Long run lands on Sunday to rehearse
# race-day timing, since the race is a Sunday morning.

# Each week: phase label, headline, and seven days keyed 0=Mon .. 6=Sun.
#
# Every day carries machine-readable fields alongside the prose, because the
# adaptive layer has to reason about sessions rather than parse sentences:
#
#   kind   rest | cross | easy | quality | medium_long | long | race
#   miles  planned run mileage (0 for rest and cross-training days)
#   key    whether this is a session the block actually depends on
#   lift   upper_push | upper_pull | legs | mobility | None
#
# Long runs land on Sunday to rehearse race-day timing, since the race is a
# Sunday morning. Lifting sits on easy or no-run days and never the day before a
# key run; leg days are placed to protect against running injury, not to chase
# strength numbers.

WEEKS = [
    {
        "phase": "Week 1 — Re-entry",
        "headline": "Post-70.3 transition. Legs back under you, no quality yet.",
        "days": {
            0: {"session": "Rest or 30min easy spin. Nothing hard.", "target": "Full recovery",
                "kind": "rest", "miles": 0, "key": False, "lift": None},
            1: {"session": "Easy 5mi + 4×20s strides", "target": f"{EASY}/mi · strides relaxed",
                "kind": "easy", "miles": 5, "key": False, "lift": None},
            2: {"session": "Swim 1500m easy + Upper Pull lift", "target": "Easy · flush the legs",
                "kind": "cross", "miles": 0, "key": False, "lift": "upper_pull"},
            3: {"session": "Easy 6mi steady", "target": f"{EASY}/mi · HR <150",
                "kind": "easy", "miles": 6, "key": False, "lift": None},
            4: {"session": "Rest or 40min easy spin", "target": "Recovery",
                "kind": "rest", "miles": 0, "key": False, "lift": None},
            5: {"session": "Easy 4mi + 6×20s strides", "target": f"{EASY}/mi",
                "kind": "easy", "miles": 4, "key": False, "lift": "upper_push"},
            6: {"session": "Long run 9mi easy — first real long run back",
                "target": f"{LONG}/mi · HR <155", "kind": "long", "miles": 9, "key": True, "lift": None},
        },
    },
    {
        "phase": "Week 2 — Build 1",
        "headline": "Volume returns. First structured quality session of the block.",
        "days": {
            0: {"session": "Recovery: 30min easy spin or swim 1500m + Upper Push lift",
                "target": "Easy", "kind": "cross", "miles": 0, "key": False, "lift": "upper_push"},
            1: {"session": f"Quality — Intervals: 1.5 WU / 5×1000m @ {INT} (2min jog) / 1 CD",
                "target": f"{INT}/mi reps · Z4", "kind": "quality", "miles": 6, "key": True, "lift": None},
            2: {"session": "Easy 4mi + Legs (moderate)", "target": f"{EASY}/mi",
                "kind": "easy", "miles": 4, "key": False, "lift": "legs"},
            3: {"session": f"Medium-long 7mi, last 2mi @ {MP}", "target": f"Finish at MP {MP}/mi",
                "kind": "medium_long", "miles": 7, "key": True, "lift": None},
            4: {"session": "Rest or swim 1800m easy", "target": "Recovery",
                "kind": "rest", "miles": 0, "key": False, "lift": None},
            5: {"session": "Easy 3mi + 6×20s strides", "target": f"{EASY}/mi",
                "kind": "easy", "miles": 3, "key": False, "lift": "upper_pull"},
            6: {"session": "Long run 11mi easy", "target": f"{LONG}/mi · HR <155",
                "kind": "long", "miles": 11, "key": True, "lift": None},
        },
    },
    {
        "phase": "Week 3 — Build 2",
        "headline": "Threshold block. This is where goal pace starts feeling cheap.",
        "days": {
            0: {"session": "Recovery: 30min spin + Upper Pull lift", "target": "Easy",
                "kind": "cross", "miles": 0, "key": False, "lift": "upper_pull"},
            1: {"session": f"Quality — Tempo: 1.5 WU / 5mi @ {THR} / 1 CD",
                "target": f"{THR}/mi · Z3–low Z4", "kind": "quality", "miles": 7, "key": True, "lift": None},
            2: {"session": "Easy 4mi + Legs (moderate)", "target": f"{EASY}/mi",
                "kind": "easy", "miles": 4, "key": False, "lift": "legs"},
            3: {"session": f"Medium-long 8mi, middle 3mi @ {MP}", "target": f"MP {MP}/mi",
                "kind": "medium_long", "miles": 8, "key": True, "lift": None},
            4: {"session": "Rest or swim 1800m", "target": "Recovery",
                "kind": "rest", "miles": 0, "key": False, "lift": None},
            5: {"session": "Easy 3mi + strides", "target": f"{EASY}/mi",
                "kind": "easy", "miles": 3, "key": False, "lift": "upper_push"},
            6: {"session": "Long run 13mi easy", "target": f"{LONG}/mi · HR <155",
                "kind": "long", "miles": 13, "key": True, "lift": None},
        },
    },
    {
        "phase": "Week 4 — Build 3",
        "headline": "Marathon pace goes into the long run. Fuelling rehearsal starts.",
        "days": {
            0: {"session": "Recovery: swim 1500m + Upper Push lift", "target": "Easy",
                "kind": "cross", "miles": 0, "key": False, "lift": "upper_push"},
            1: {"session": f"Quality — Intervals: 1.5 WU / 6×1mi @ {THR} (90s jog) / 1 CD",
                "target": f"{THR}/mi · controlled", "kind": "quality", "miles": 8, "key": True, "lift": None},
            2: {"session": "Easy 4mi + Legs", "target": f"{EASY}/mi",
                "kind": "easy", "miles": 4, "key": False, "lift": "legs"},
            3: {"session": f"Medium-long 9mi, 2×2mi @ {MP} (1mi easy between)",
                "target": f"MP {MP}/mi", "kind": "medium_long", "miles": 9, "key": True, "lift": None},
            4: {"session": "Rest or 45min easy spin", "target": "Recovery",
                "kind": "rest", "miles": 0, "key": False, "lift": None},
            5: {"session": "Easy 3mi + strides", "target": f"{EASY}/mi",
                "kind": "easy", "miles": 3, "key": False, "lift": "upper_pull"},
            6: {"session": f"LONG RUN 15mi — last 4mi @ {MP}. Practice race fuelling: 60g carbs/hr from mile 4.",
                "target": f"Easy → MP {MP}/mi", "kind": "long", "miles": 15, "key": True, "lift": None},
        },
    },
    {
        "phase": "Week 5 — Peak 1",
        "headline": "Biggest aerobic week. Bank the volume, stay off the red line.",
        "days": {
            0: {"session": "Recovery: 30min spin + Upper Pull lift", "target": "Easy",
                "kind": "cross", "miles": 0, "key": False, "lift": "upper_pull"},
            1: {"session": f"Quality — Tempo: 1.5 WU / 6mi @ {THR} / 1 CD", "target": f"{THR}/mi",
                "kind": "quality", "miles": 8, "key": True, "lift": None},
            2: {"session": "Easy 5mi + Legs (lighter)", "target": f"{EASY}/mi",
                "kind": "easy", "miles": 5, "key": False, "lift": "legs"},
            3: {"session": f"Medium-long 10mi, middle 5mi @ {MP}", "target": f"MP {MP}/mi",
                "kind": "medium_long", "miles": 10, "key": True, "lift": None},
            4: {"session": "Rest or swim 2000m easy", "target": "Recovery",
                "kind": "rest", "miles": 0, "key": False, "lift": None},
            5: {"session": "Easy 3mi + strides", "target": f"{EASY}/mi",
                "kind": "easy", "miles": 3, "key": False, "lift": "upper_push"},
            6: {"session": "LONG RUN 18mi steady. Full fuelling rehearsal: 60–75g carbs/hr, drink every 20min.",
                "target": f"{LONG}/mi · HR <155", "kind": "long", "miles": 18, "key": True, "lift": None},
        },
    },
    {
        "phase": "Week 6 — Peak 2",
        "headline": "Race simulation week. The 20-miler is the whole block in one session.",
        "days": {
            0: {"session": "Recovery: swim 1500m + Upper Push lift", "target": "Easy",
                "kind": "cross", "miles": 0, "key": False, "lift": "upper_push"},
            1: {"session": f"Quality — Intervals: 1.5 WU / 3×2mi @ {THR} (2min jog) / 1 CD",
                "target": f"{THR}/mi", "kind": "quality", "miles": 8, "key": True, "lift": None},
            2: {"session": "Easy 5mi + Legs (light)", "target": f"{EASY}/mi",
                "kind": "easy", "miles": 5, "key": False, "lift": "legs"},
            3: {"session": f"Medium-long 10mi, 6mi @ {MP} — dress rehearsal pace feel",
                "target": f"MP {MP}/mi · should feel repeatable", "kind": "medium_long",
                "miles": 10, "key": True, "lift": None},
            4: {"session": "Rest — full day off legs", "target": "Rest",
                "kind": "rest", "miles": 0, "key": False, "lift": None},
            5: {"session": "Easy 3mi + strides", "target": f"{EASY}/mi",
                "kind": "easy", "miles": 3, "key": False, "lift": None},
            6: {"session": f"LONG RUN 20mi — race kit, race shoes, race breakfast, race fuelling. Last 5mi @ {MP}.",
                "target": f"Easy → MP {MP}/mi · the key session", "kind": "long",
                "miles": 20, "key": True, "lift": None},
        },
    },
    {
        "phase": "Week 7 — Taper 1",
        "headline": "Volume drops ~25%, intensity stays. You should start feeling springy.",
        "days": {
            0: {"session": "Recovery: 30min spin + Upper Pull (70% load)", "target": "Easy",
                "kind": "cross", "miles": 0, "key": False, "lift": "upper_pull"},
            1: {"session": f"Quality — 1.5 WU / 4mi @ {THR} / 1 CD", "target": f"{THR}/mi · sharp not deep",
                "kind": "quality", "miles": 6, "key": True, "lift": None},
            2: {"session": "Easy 4mi (no heavy legs from here)", "target": f"{EASY}/mi",
                "kind": "easy", "miles": 4, "key": False, "lift": "mobility"},
            3: {"session": f"Medium-long 8mi, 4mi @ {MP}", "target": f"MP {MP}/mi",
                "kind": "medium_long", "miles": 8, "key": True, "lift": None},
            4: {"session": "Rest or swim 1500m easy", "target": "Recovery",
                "kind": "rest", "miles": 0, "key": False, "lift": None},
            5: {"session": "Easy 3mi + 6×20s strides", "target": f"{EASY}/mi",
                "kind": "easy", "miles": 3, "key": False, "lift": None},
            6: {"session": f"Long run 13mi, last 3mi @ {MP}", "target": f"{LONG} → MP {MP}/mi",
                "kind": "long", "miles": 13, "key": True, "lift": None},
        },
    },
    {
        "phase": "Week 8 — Race Week",
        "headline": "Deep taper. Nothing this week makes you fitter — only fresher.",
        "days": {
            0: {"session": "Easy 5mi relaxed + 4×20s strides", "target": f"{EASY}/mi",
                "kind": "easy", "miles": 5, "key": False, "lift": "mobility"},
            1: {"session": f"Sharpener: 1 WU / 3×1mi @ {MP} (2min jog) / 1 CD",
                "target": f"MP {MP}/mi · locks in rhythm", "kind": "quality", "miles": 5,
                "key": True, "lift": None},
            2: {"session": "Easy 4mi. Carb-load begins — ramp to 8–10g/kg/day.",
                "target": f"{EASY}/mi · hydrate", "kind": "easy", "miles": 4, "key": False, "lift": None},
            3: {"session": "Easy 3mi + 4 strides. Lay out race kit.", "target": "Very easy",
                "kind": "easy", "miles": 3, "key": False, "lift": None},
            4: {"session": "Rest or 20min shakeout jog. Travel / packet pickup. Off legs by evening.",
                "target": "Rest", "kind": "rest", "miles": 0, "key": False, "lift": None},
            5: {"session": "20min easy jog + 3 strides. Final carb-load day. Early night.",
                "target": "Prime · rest", "kind": "easy", "miles": 2, "key": False, "lift": None},
            6: {"session": f"RACE DAY · MARATHON · goal {GOAL_TIME} ({MP}/mi). Fuel 60–75g carbs/hr from mile 4.",
                "target": "EXECUTE THE PLAN", "kind": "race", "miles": 26.2, "key": True, "lift": None},
        },
    },
]

# Sessions matching these keywords are treated as the block's signature
# sessions -- the ones adherence and the coach layer actually care about.
KEY_SESSION_KEYWORDS = ["long run", "race day", "quality", "tempo", "intervals", "sharpener"]

FUEL = {
    "race_day": {
        "carbs_per_hour": "60–75g",
        "fluid": "500–750ml/hr, more if warm",
        "sodium": "300–600mg/hr",
        "pre_race": "2–2.5g/kg carbs, 3hrs out; nothing new on race morning",
        "notes": "You rehearsed 60–90g/hr on the 70.3 bike — running tolerates less than riding. Hold 60–75g/hr and practice it on every long run from Week 4.",
    },
    "carb_load": {
        "window": "72hrs out (Thu–Sat of race week)",
        "target": "8–10g carbs per kg bodyweight per day",
        "notes": "Volume is already low in race week, so the extra carbs go to glycogen rather than fatigue.",
    },
    "daily_baseline": {
        "protein": "1.6–2.0g/kg/day",
        "carbs_easy_day": "4–5g/kg",
        "carbs_quality_day": "6–7g/kg",
        "carbs_long_run_day": "8g/kg",
    },
}


def build() -> dict:
    days = {}
    # Weekly volume is the sum of the week's days rather than a number typed in
    # beside them, so the target can never drift out of step with the sessions.
    week_totals = [round(sum(d["miles"] for d in w["days"].values()), 1) for w in WEEKS]

    for week_index, week in enumerate(WEEKS):
        week_start = BLOCK_START + timedelta(weeks=week_index)
        for weekday, spec in week["days"].items():
            day = week_start + timedelta(days=weekday)
            days[day.isoformat()] = {
                **spec,
                "phase": week["phase"],
                "week": week_index + 1,
                "week_miles_target": week_totals[week_index],
            }

    weeks_meta = [
        {
            "week": i + 1,
            "phase": w["phase"],
            "headline": w["headline"],
            "miles_target": week_totals[i],
            "start": (BLOCK_START + timedelta(weeks=i)).isoformat(),
            "end": (BLOCK_START + timedelta(weeks=i, days=6)).isoformat(),
        }
        for i, w in enumerate(WEEKS)
    ]

    return {
        "race": {
            "name": RACE_NAME,
            "date": RACE_DATE.isoformat(),
            "distance": "Marathon (26.2mi)",
            "goal": f"Sub-{GOAL_TIME} ({MP}/mi)",
            "goal_seconds": parse_goal_seconds(GOAL_TIME),
            "start_time": "Morning start",
        },
        "block": {
            "start": BLOCK_START.isoformat(),
            "end": (BLOCK_START + timedelta(weeks=len(WEEKS), days=-1)).isoformat(),
            "weeks": len(WEEKS),
            "context": (
                "Follows IRONMAN 70.3 Wisconsin (Sep 12, 2026). The aerobic engine is "
                "already built; this block converts it into run-specific volume and "
                "marathon-pace durability. Week 1 is re-entry, not training."
            ),
        },
        "paces": PACES,
        "weeks": weeks_meta,
        "key_session_keywords": KEY_SESSION_KEYWORDS,  # kept for older consumers
        "fuel": FUEL,
        "days": days,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "generated_by": "plan/build_plan.py",
    }


if __name__ == "__main__":
    plan = build()
    OUT.write_text(json.dumps(plan, indent=2))
    print(f"Wrote {OUT} — {len(plan['days'])} days, {plan['block']['weeks']} weeks")
    print(f"Goal {GOAL_TIME} -> marathon pace {MP}/mi")
    print("  weekly volume: " + " → ".join(str(w["miles_target"]) for w in plan["weeks"]) + " mi")
    for key, value in PACES.items():
        if not key.endswith("_seconds"):
            print(f"  {key:10s} {value}/mi")
