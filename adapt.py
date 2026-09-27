"""
Matches what actually happened against the plan, and adapts the next two days.

Two jobs, kept separate:

**Matching** compares recorded activities against planned sessions, so "done" is
a measured fact rather than a checkbox. A run on the planned day covering at
least 60% of the planned distance counts as done; less than that is cut short; a
run on a different day fills the nearest unmatched run in the same week, because
shifting a session by a day is normal life and shouldn't read as a miss.

**Adaptation** adjusts only today and tomorrow, and never rewrites the base plan.
The base plan in plan/plan.json is fixed -- generated once from the goal time and
left alone -- so the dashboard can always show the original session next to the
suggestion and let Frank decide which to do. A plan that quietly rewrites itself
every morning is a plan you stop trusting.

The triggers, in the order they are applied:

  readiness >= 70              take it as planned
  readiness 45-69              quality becomes easy at the same distance;
                               long run cut 10%; leg day drops to 2 sets
  readiness < 45               rest or 30min very easy; a key session is moved to
                               a later easy day this week, never the day before
                               another key session; lifting becomes mobility
  3 of last 4 days < 55        absorb week: the rest of the week's runs cut 20%
  fatigue/fitness > 1.4        the next quality session becomes easy
  2+ key sessions missed       this week held at last week's volume

Usage:
  python3 adapt.py           # annotates web/data.json in place
"""

import argparse
import json
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

BASE = Path(__file__).resolve().parent
DATA_JSON = BASE / "web" / "data.json"

MATCH_THRESHOLD = 0.60          # a run counts as done at 60% of planned distance
EASY_KINDS = {"easy", "cross", "rest"}
RUNNING_KINDS = {"easy", "quality", "medium_long", "long", "race"}


def parse_date(iso: str) -> date:
    return datetime.fromisoformat(iso).date()


# --------------------------------------------------------------------------
# matching
# --------------------------------------------------------------------------

def match_plan(plan_days: dict, runs: list, as_of: date) -> dict:
    """Work out which planned sessions actually happened.

    ``runs`` is a list of {date, miles} for recorded runs. Returns a per-day
    status map plus the unmatched runs, which are the ones that filled a
    different day or were extra.
    """
    runs_by_date = {}
    for run in runs:
        runs_by_date.setdefault(run["date"], []).append(run)

    statuses = {}
    used_dates = set()

    # Pass 1: same-day matches.
    for iso, entry in plan_days.items():
        planned = entry.get("miles") or 0
        day = parse_date(iso)
        if day > as_of:
            statuses[iso] = {"status": "upcoming", "actual_miles": 0.0}
            continue
        if planned <= 0:
            # A rest or cross-training day: anything recorded is a bonus, not a
            # miss. Deliberately NOT marked as used, so pass 2 can still count
            # that run against a session missed elsewhere in the week -- running
            # on Monday instead of Thursday is a shifted session, not a bonus
            # run plus a failure.
            recorded = sum(r["miles"] for r in runs_by_date.get(iso, []))
            statuses[iso] = {"status": "rest", "actual_miles": round(recorded, 1)}
            continue

        # Today is still in progress: a session planned for today has not been
        # missed yet, and calling it missed makes the dashboard scold you at
        # breakfast for a run you're about to do.
        if day == as_of:
            actual_today = sum(r["miles"] for r in runs_by_date.get(iso, []))
            if actual_today >= planned * MATCH_THRESHOLD:
                statuses[iso] = {"status": "done", "actual_miles": round(actual_today, 1),
                                 "planned_miles": planned,
                                 "pct": round(100 * actual_today / planned)}
                used_dates.add(iso)
            else:
                statuses[iso] = {"status": "today", "actual_miles": round(actual_today, 1),
                                 "planned_miles": planned}
                if actual_today:
                    used_dates.add(iso)
            continue

        actual = sum(r["miles"] for r in runs_by_date.get(iso, []))
        if actual >= planned * MATCH_THRESHOLD:
            statuses[iso] = {"status": "done", "actual_miles": round(actual, 1),
                             "planned_miles": planned,
                             "pct": round(100 * actual / planned)}
            used_dates.add(iso)
        elif actual > 0:
            statuses[iso] = {"status": "short", "actual_miles": round(actual, 1),
                             "planned_miles": planned,
                             "pct": round(100 * actual / planned)}
            used_dates.add(iso)
        else:
            statuses[iso] = {"status": "missed", "actual_miles": 0.0,
                             "planned_miles": planned, "pct": 0}

    # Pass 2: a run on the wrong day fills the nearest miss in the same week.
    for iso, runs_that_day in sorted(runs_by_date.items()):
        if iso in used_dates or parse_date(iso) > as_of:
            continue
        # A run that already satisfied its own planned session is not available
        # to fill another day.
        if statuses.get(iso, {}).get("status") in ("done", "short", "moved"):
            continue
        day = parse_date(iso)
        week_start = day - timedelta(days=day.weekday())
        week_end = week_start + timedelta(days=6)

        candidates = [
            (abs((parse_date(k) - day).days), k)
            for k, v in statuses.items()
            if v["status"] == "missed" and week_start <= parse_date(k) <= week_end
        ]
        if not candidates:
            continue
        _, target = min(candidates)
        moved = sum(r["miles"] for r in runs_that_day)
        planned = statuses[target].get("planned_miles") or 0
        statuses[target] = {
            "status": "moved" if moved >= planned * MATCH_THRESHOLD else "short",
            "actual_miles": round(moved, 1),
            "planned_miles": planned,
            "pct": round(100 * moved / planned) if planned else None,
            "moved_from": iso,
        }
        # The mileage now belongs to the day it filled, so it must not also be
        # counted where it was run -- otherwise a run shifted by a day inflates
        # the week's total by its own distance.
        source = statuses.get(iso)
        if source is not None:
            source["actual_miles"] = 0.0
            source["moved_to"] = target
            source["moved_miles"] = round(moved, 1)
        used_dates.add(iso)

    return statuses


def week_summary(plan_days: dict, statuses: dict, week_start: str) -> dict:
    """Planned versus done for one week, by session and by mileage."""
    start = parse_date(week_start)
    days = [(start + timedelta(days=i)).isoformat() for i in range(7)]

    planned_miles = sum((plan_days.get(d, {}).get("miles") or 0) for d in days)
    actual_miles = sum((statuses.get(d, {}).get("actual_miles") or 0) for d in days)
    key_days = [d for d in days if (plan_days.get(d) or {}).get("key")]
    key_done = [d for d in key_days
                if statuses.get(d, {}).get("status") in ("done", "moved")]
    # "today" is excluded: the session hasn't happened yet, so it is neither
    # done nor missed.
    key_missed = [d for d in key_days if statuses.get(d, {}).get("status") == "missed"]
    key_pending = [d for d in key_days if statuses.get(d, {}).get("status") == "today"]

    return {
        "week_start": week_start,
        "planned_miles": round(planned_miles, 1),
        "actual_miles": round(actual_miles, 1),
        "key_planned": len(key_days),
        "key_done": len(key_done),
        "key_missed": len(key_missed),
        "key_pending": len(key_pending),
    }


# --------------------------------------------------------------------------
# adaptation
# --------------------------------------------------------------------------

def _easy_version(entry: dict, paces: dict) -> dict:
    """Same distance, all of it easy. The volume is the point, not the intensity."""
    miles = entry.get("miles") or 0
    return {
        "session": f"Easy {miles:g}mi — quality pulled, keep the distance",
        "target": f"{paces.get('easy', 'easy')}/mi · conversational",
        "kind": "easy", "miles": miles, "key": entry.get("key", False),
        "lift": entry.get("lift"),
    }


def _reduced_long(entry: dict, paces: dict, factor=0.9) -> dict:
    miles = round((entry.get("miles") or 0) * factor, 1)
    return {
        "session": f"Long run {miles:g}mi easy — trimmed {int((1 - factor) * 100)}% for recovery",
        "target": f"{paces.get('long', 'easy')}/mi",
        "kind": "long", "miles": miles, "key": entry.get("key", False), "lift": None,
    }


def _rest_version(entry: dict) -> dict:
    return {
        "session": "Rest, or 30min very easy. Recovery signals say today is not the day.",
        "target": "Very easy or nothing at all",
        "kind": "rest", "miles": 0, "key": False,
        "lift": "mobility" if entry.get("lift") else None,
    }


def _scale_lift(entry: dict, adapted: dict, level: str) -> None:
    """Lifting scales down with recovery instead of being dropped outright."""
    lift = entry.get("lift")
    if not lift:
        return
    if level == "low":
        adapted["lift"] = "mobility"
        adapted["lift_note"] = f"{lift.replace('_', ' ')} → mobility only"
    elif level == "moderate":
        adapted["lift"] = lift
        adapted["lift_note"] = ("2 sets instead of 3, no Nordics"
                                if lift == "legs" else "2 sets, lighter")


def find_reschedule_slot(plan_days: dict, statuses: dict, from_iso: str) -> str:
    """Where a dropped key session could go instead.

    Rules: later in the same week, on a day that isn't already a key session, and
    never the day immediately before another key session -- moving a hard run
    onto the eve of the long run just moves the problem.
    """
    day = parse_date(from_iso)
    week_end = day - timedelta(days=day.weekday()) + timedelta(days=6)

    candidate = day + timedelta(days=1)
    while candidate <= week_end:
        iso = candidate.isoformat()
        entry = plan_days.get(iso) or {}
        next_iso = (candidate + timedelta(days=1)).isoformat()
        next_is_key = (plan_days.get(next_iso) or {}).get("key")
        if entry.get("kind") in EASY_KINDS and not entry.get("key") and not next_is_key:
            return iso
        candidate += timedelta(days=1)
    return None


def adapt(data: dict, statuses: dict) -> dict:
    """Produce today's and tomorrow's suggestions, with reasons.

    Returns the adaptation record: per-day original and suggested sessions, the
    triggers that fired, and any week-level adjustment. Nothing here mutates the
    base plan.
    """
    plan_days = data.get("plan_days") or {}
    paces = data.get("paces") or {}
    readiness = (data.get("readiness") or {}).get("score")
    history = (data.get("recovery") or {}).get("history") or []
    form = data.get("form") or {}
    as_of = parse_date(data["meta"]["as_of"])

    triggers = []

    # --- level from today's readiness ---
    if readiness is None:
        level = "unknown"
    elif readiness >= 70:
        level = "green"
    elif readiness >= 45:
        level = "moderate"
    else:
        level = "low"

    if level == "green":
        triggers.append({"trigger": "readiness >= 70", "effect": "Take the plan as written.",
                         "severity": "good"})
    elif level == "moderate":
        triggers.append({"trigger": f"readiness {readiness} (45–69)",
                         "effect": "Quality becomes easy at the same distance; long run trimmed 10%; "
                                   "lifting drops a set.", "severity": "warning"})
    elif level == "low":
        triggers.append({"trigger": f"readiness {readiness} (< 45)",
                         "effect": "Rest or 30min very easy. A key session moves later in the week; "
                                   "lifting becomes mobility only.", "severity": "serious"})

    # --- absorb week: three of the last four days below 55 ---
    recent_scores = [h.get("readiness_score") for h in history[-4:]
                     if h.get("readiness_score") is not None]
    absorb = False
    if len(recent_scores) >= 4 and sum(1 for s in recent_scores if s < 55) >= 3:
        absorb = True
        triggers.append({"trigger": "3 of the last 4 days below 55",
                         "effect": "Absorb week — the rest of this week's running cut 20%.",
                         "severity": "serious"})

    # --- fatigue running ahead of fitness ---
    fitness, fatigue = form.get("fitness"), form.get("fatigue")
    ratio = (fatigue / fitness) if (fitness and fatigue and fitness > 0) else None
    if ratio and ratio > 1.4:
        triggers.append({"trigger": f"fatigue/fitness {ratio:.2f} (> 1.4)",
                         "effect": "The next quality session becomes easy.", "severity": "warning"})

    # --- last week's key sessions ---
    last_week_start = (as_of - timedelta(days=as_of.weekday() + 7)).isoformat()
    last_week = week_summary(plan_days, statuses, last_week_start)
    hold_volume = last_week["key_missed"] >= 2
    if hold_volume:
        triggers.append({
            "trigger": f"{last_week['key_missed']} key sessions missed last week",
            "effect": f"Hold this week at last week's volume "
                      f"({last_week['actual_miles']:g}mi) rather than progressing.",
            "severity": "warning"})

    # --- build the per-day suggestions ---
    suggestions = {}
    for offset, label in ((0, "today"), (1, "tomorrow")):
        iso = (as_of + timedelta(days=offset)).isoformat()
        original = plan_days.get(iso)
        if not original:
            continue

        adapted, reasons = None, []

        if level == "low" and original.get("kind") in RUNNING_KINDS and original.get("miles", 0) > 0:
            adapted = _rest_version(original)
            reasons.append("readiness below 45")
            if original.get("key"):
                slot = find_reschedule_slot(plan_days, statuses, iso)
                adapted["move_to"] = slot
                adapted["session"] += (
                    f" Move this key session to {slot}." if slot
                    else " No clean slot left this week — this one is a write-off, and that's fine.")
        elif level == "moderate":
            if original.get("kind") in ("quality", "medium_long"):
                adapted = _easy_version(original, paces)
                reasons.append("readiness 45–69")
            elif original.get("kind") == "long":
                adapted = _reduced_long(original, paces)
                reasons.append("readiness 45–69")
        elif ratio and ratio > 1.4 and original.get("kind") == "quality":
            adapted = _easy_version(original, paces)
            reasons.append(f"fatigue/fitness {ratio:.2f}")

        # Week-level cuts stack on top of whatever the day already became.
        if absorb and (adapted or original).get("miles", 0) > 0:
            target = dict(adapted or original)
            target["miles"] = round(target["miles"] * 0.8, 1)
            target["session"] = f"{target['session']} (cut 20% — absorb week)"
            adapted = target
            reasons.append("absorb week")

        if adapted:
            _scale_lift(original, adapted, "low" if level == "low" else "moderate")

        suggestions[label] = {
            "date": iso,
            "original": original,
            "suggested": adapted,
            "changed": adapted is not None,
            "reasons": reasons,
        }

    return {
        "level": level,
        "readiness": readiness,
        "triggers": triggers,
        "suggestions": suggestions,
        "last_week": last_week,
        "hold_volume": hold_volume,
        "absorb_week": absorb,
        "fatigue_fitness_ratio": round(ratio, 2) if ratio else None,
        "note": "The base plan is never rewritten. These are suggestions shown next to the "
                "original session — you pick which one you do.",
    }


# --------------------------------------------------------------------------
# entry point
# --------------------------------------------------------------------------

def annotate(data: dict) -> dict:
    """Add matching and adaptation to a signals payload."""
    runs = [{"date": r["date"], "miles": r["miles"]}
            for r in (data.get("fitness") or {}).get("all_runs", [])
            or (data.get("fitness") or {}).get("recent_runs", [])]
    as_of = parse_date(data["meta"]["as_of"])
    plan_days = data.get("plan_days") or {}

    statuses = match_plan(plan_days, runs, as_of)
    data["matching"] = statuses
    data["adaptation"] = adapt(data, statuses)

    # Restate weekly adherence from the matching, which is stricter than
    # "an activity happened that day".
    for week in (data.get("adherence") or {}).get("weeks", []):
        summary = week_summary(plan_days, statuses, week["start"])
        week["miles_actual"] = summary["actual_miles"]
        week["key_sessions_due"] = summary["key_planned"]
        week["key_sessions_done"] = summary["key_done"]
        week["key_sessions_missed"] = summary["key_missed"]

    weeks = (data.get("adherence") or {}).get("weeks", [])
    elapsed = [w for w in weeks if w["status"] in ("done", "current")]
    due = sum(w["key_sessions_due"] for w in elapsed)
    done = sum(w["key_sessions_done"] for w in elapsed)
    if data.get("adherence") is not None:
        data["adherence"]["key_sessions_due_to_date"] = due
        data["adherence"]["key_sessions_done_to_date"] = done
        data["adherence"]["completion_pct"] = round(100 * done / due) if due else None

    return data


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data", default=str(DATA_JSON))
    args = parser.parse_args()

    path = Path(args.data)
    if not path.exists():
        print(f"{path} not found — run signals.py first.", file=sys.stderr)
        sys.exit(1)

    data = annotate(json.loads(path.read_text()))
    path.write_text(json.dumps(data, indent=2, default=str))

    adaptation = data["adaptation"]
    print(f"Adaptation written ({adaptation['level']}, readiness {adaptation['readiness']}):")
    for trigger in adaptation["triggers"]:
        print(f"  [{trigger['severity']}] {trigger['trigger']} → {trigger['effect']}")
    for label, suggestion in adaptation["suggestions"].items():
        if suggestion["changed"]:
            print(f"  {label}: {suggestion['original']['session']}")
            print(f"    → {suggestion['suggested']['session']}")
        else:
            print(f"  {label}: as planned — {suggestion['original']['session'][:70]}")

    statuses = data["matching"]
    counts = {}
    for value in statuses.values():
        counts[value["status"]] = counts.get(value["status"], 0) + 1
    print(f"  matching: {counts}")


if __name__ == "__main__":
    main()
