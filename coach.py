"""
Writes the coach's read into web/data.json.

Two modes, and the default one makes no network call:

  rules  (default) -- a deterministic rules engine over the computed signals.
                      The same inputs always produce the same read, it costs
                      nothing, it works offline, and it can be unit-tested.
  claude (--live)  -- hands the *computed signals* to Claude and asks for the
                      read in plain language.

The division of labour is the point. The model never sees raw Garmin payloads;
it sees readiness, workload ratio, resting-HR deviation, adherence and the
projection -- numbers this pipeline already computed and can defend. That keeps
the prompt small, keeps the model off arithmetic it would do badly, and means
the interesting analysis is reproducible rather than resampled on every run.

Usage:
  python3 coach.py                  # rules engine
  python3 coach.py --live           # ask Claude, falling back to rules on failure
"""

import argparse
import json
import os
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent
DATA_JSON = BASE / "web" / "data.json"

MODEL = "claude-sonnet-5"

SEVERITY_ORDER = {"good": 0, "moderate": 1, "warning": 1, "serious": 2, "critical": 3}


def build_rules_note(d: dict) -> dict:
    """Deterministic read of the computed signals.

    Each rule fires on one signal and contributes a line plus a severity; the
    headline is the most severe thing that fired. Rules reference the athlete's
    own baselines rather than population norms, because a resting HR of 54 means
    nothing until you know his median is 48.
    """
    notes, severities = [], []

    readiness = d.get("readiness") or {}
    rhr = (d.get("recovery") or {}).get("resting_hr") or {}
    sleep = (d.get("recovery") or {}).get("sleep") or {}
    load = d.get("load") or {}
    adherence = d.get("adherence") or {}
    projection = d.get("projection") or {}
    today_plan = (d.get("today") or {}).get("plan") or {}
    race = d.get("race") or {}

    # --- resting heart rate against his own baseline ---
    delta, z = rhr.get("delta"), rhr.get("z")
    if delta is not None and rhr.get("baseline") is not None:
        if delta >= 5:
            severities.append("serious")
            notes.append(
                f"Resting HR is {rhr['today']} against a 28-day baseline of {rhr['baseline']} "
                f"(+{delta:.0f} bpm{f', {z:.1f} SD above normal' if z else ''}). That is the clearest "
                "signal on the page today — treat it as real until it comes back down."
            )
        elif delta >= 3:
            severities.append("warning")
            notes.append(f"Resting HR is {delta:.0f} bpm above your 28-day baseline of {rhr['baseline']}. "
                         "Worth watching for a second day before reading anything into it.")
        elif delta <= -2:
            severities.append("good")
            notes.append(f"Resting HR is {abs(delta):.0f} bpm below your 28-day baseline — you're absorbing the work.")

    # --- sleep ---
    if sleep.get("avg_7day_hours") is not None:
        target = sleep.get("target_hours", 7.5)
        avg, debt = sleep["avg_7day_hours"], sleep.get("debt_7day_hours")
        if avg < target - 1:
            severities.append("warning")
            notes.append(f"Averaging {avg}h a night over the last week against a {target}h target"
                         f"{f' — about {debt:.0f}h of accumulated debt' if debt and debt > 2 else ''}.")
        elif avg >= target:
            severities.append("good")
            notes.append(f"Sleep is averaging {avg}h against a {target}h target — nothing to fix.")

    # --- workload ---
    zone, ratio = load.get("zone"), load.get("ratio")
    if zone == "high_risk":
        severities.append("serious")
        notes.append(f"Workload ratio is {ratio} — a sharp spike over your 4-week baseline. "
                     "Hold this week's volume where it is; the adaptation is already banked.")
    elif zone == "caution":
        severities.append("warning")
        notes.append(f"Workload ratio is {ratio}, ramping a little faster than your baseline supports. "
                     "Fine for one week, not for three.")
    elif zone == "detraining":
        severities.append("warning")
        notes.append(f"Workload ratio is {ratio} — you are carrying less load than your 4-week baseline. "
                     "Expected straight after a race, but the marathon block needs that number climbing "
                     "back toward 1.0 over the next fortnight.")
    elif zone == "optimal":
        severities.append("good")
        notes.append(f"Workload ratio is {ratio}, squarely in the productive range.")

    if load.get("monotony") and load["monotony"] > 2.0:
        severities.append("warning")
        notes.append(f"Training monotony is {load['monotony']} — every day is looking alike. "
                     "Make the easy days properly easy so the hard ones can be hard.")

    # --- adherence ---
    pct = adherence.get("completion_pct")
    if pct is not None:
        due = adherence.get("key_sessions_due_to_date")
        done = adherence.get("key_sessions_done_to_date")
        if pct >= 90:
            severities.append("good")
            notes.append(f"{done} of {due} key sessions completed so far — the block is on the rails.")
        elif pct >= 60:
            severities.append("warning")
            notes.append(f"{done} of {due} key sessions completed. The long runs are the ones that matter; "
                         "protect those before the midweek quality.")
        else:
            severities.append("serious")
            notes.append(f"Only {done} of {due} key sessions completed. At this rate the goal pace will "
                         "outrun the training — either the plan needs trimming or the week does.")

    # --- projection ---
    # How alarming a pace gap is depends entirely on how much block is left. A
    # 20-minute gap seven weeks out is the normal starting position; the same gap
    # in race week is the story of the day. So the severity is scaled by the
    # calendar rather than by the gap alone.
    gap = projection.get("gap_seconds")
    days_out = race.get("days_to_race")
    if gap is not None and projection.get("predicted"):
        if gap <= 0:
            severities.append("good")
            notes.append(f"Recent runs project to {projection['predicted']}, at or inside the goal.")
        else:
            minutes = gap / 60
            if days_out is None or days_out > 42:
                severity, framing = ("good" if minutes <= 25 else "warning"), (
                    f"with {days_out} days of block left, that is a normal place to be starting from"
                    if days_out else "early in the block")
            elif days_out > 21:
                severity, framing = ("warning" if minutes <= 20 else "serious"), (
                    f"{days_out} days out, the marathon-pace work should be closing this now")
            else:
                severity, framing = ("warning" if minutes <= 10 else "serious"), (
                    f"only {days_out} days out — at this point the projection is close to the verdict")
            severities.append(severity)
            notes.append(
                f"Recent runs project to {projection['predicted']}, about {minutes:.0f} minutes off "
                f"{race.get('goal', 'the goal')} — {framing}. Riegel reads training runs pessimistically, "
                "so treat it as a floor rather than a forecast."
            )

    # --- today's session, reconciled with readiness ---
    band = readiness.get("band")
    session = today_plan.get("session")
    if session:
        if band in ("poor", "low"):
            notes.append(f"Today's plan is: {session} — but with readiness at {readiness.get('score')} "
                         "the honest move is to swap it for easy aerobic work and take the quality tomorrow.")
        elif band == "moderate":
            notes.append(f"Today's plan is: {session}. Start it, and let the first mile decide whether the "
                         "whole session is there.")
        else:
            notes.append(f"Today's plan is: {session}. Green light.")

    if not severities:
        return {"band": "unknown", "headline": "Not enough data yet to say anything useful.",
                "notes": notes, "source": "rules"}

    worst = max(severities, key=lambda s: SEVERITY_ORDER.get(s, 0))
    headline = {
        "good": "Everything that matters is pointing the right way.",
        "warning": "Mostly on track, with one or two things to keep an eye on.",
        "serious": "Something here needs a decision before the next hard session.",
        "critical": "Back off. The numbers are telling you to rest.",
    }[worst]
    band = {"good": "good", "warning": "moderate", "serious": "low", "critical": "poor"}[worst]

    return {"band": band, "headline": headline, "notes": notes, "source": "rules"}


# --------------------------------------------------------------------------
# optional live model call
# --------------------------------------------------------------------------

def build_claude_note(d: dict) -> dict:
    """Ask Claude to read the computed signals. Falls back to rules on any failure.

    Only the derived numbers go in the prompt -- no raw payloads, no personally
    identifying fields. If the key is missing or the call fails, the caller gets
    the rules note instead, so the pipeline never breaks because of this step.
    """
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        note = build_rules_note(d)
        note["fallback_reason"] = "ANTHROPIC_API_KEY is not set — used the rules engine."
        return note

    try:
        import anthropic
    except ImportError:
        note = build_rules_note(d)
        note["fallback_reason"] = "The anthropic package is not installed — used the rules engine."
        return note

    briefing = {
        "days_to_race": (d.get("race") or {}).get("days_to_race"),
        "goal": (d.get("race") or {}).get("goal"),
        "readiness": d.get("readiness"),
        "resting_hr": (d.get("recovery") or {}).get("resting_hr"),
        "sleep": (d.get("recovery") or {}).get("sleep"),
        "workload": {k: v for k, v in (d.get("load") or {}).items() if k != "daily"},
        "fitness": {k: v for k, v in (d.get("fitness") or {}).items() if k != "recent_runs"},
        "projection": {k: v for k, v in (d.get("projection") or {}).items()
                       if k in ("predicted", "goal", "gap", "on_track", "from_longest")},
        "adherence": {k: v for k, v in (d.get("adherence") or {}).items() if k != "weeks"},
        "today_plan": (d.get("today") or {}).get("plan"),
        "unavailable_signals": (d.get("meta") or {}).get("unavailable_signals"),
    }

    prompt = (
        "You are reading a runner's training dashboard. Every number below was computed by his "
        "own pipeline from his Garmin data — do not recompute anything, and do not invent numbers "
        "that are not here.\n\n"
        f"{json.dumps(briefing, indent=2, default=str)}\n\n"
        "Write his read for today. Respond with JSON only, matching this shape:\n"
        '{"band": "good|moderate|low|poor", "headline": "one sentence", '
        '"notes": ["2 to 5 short paragraphs, each one specific observation"]}\n\n'
        "Reference his own baselines rather than population norms. Be direct and concrete about what "
        "to do today. If a signal is unavailable, say so rather than guessing around it."
    )

    try:
        client = anthropic.Anthropic(api_key=api_key)
        response = client.messages.create(
            model=MODEL,
            max_tokens=1200,
            messages=[{"role": "user", "content": prompt}],
        )
        text = "".join(block.text for block in response.content if block.type == "text").strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[1].rsplit("```", 1)[0]
        parsed = json.loads(text)
        return {
            "band": parsed.get("band", "moderate"),
            "headline": parsed.get("headline", ""),
            "notes": parsed.get("notes", []),
            "source": "claude",
            "model": MODEL,
        }
    except Exception as e:
        note = build_rules_note(d)
        note["fallback_reason"] = f"The model call failed ({type(e).__name__}) — used the rules engine."
        return note


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--live", action="store_true",
                        help="Ask Claude instead of the rules engine (needs ANTHROPIC_API_KEY)")
    parser.add_argument("--data", default=str(DATA_JSON), help="Path to the signals file")
    args = parser.parse_args()

    path = Path(args.data)
    if not path.exists():
        print(f"{path} not found — run signals.py first.", file=sys.stderr)
        sys.exit(1)

    data = json.loads(path.read_text())
    note = build_claude_note(data) if args.live else build_rules_note(data)
    data["coach"] = note
    path.write_text(json.dumps(data, indent=2, default=str))

    print(f"Coach note written ({note['source']}): {note['headline']}")
    if note.get("fallback_reason"):
        print(f"  {note['fallback_reason']}")
    for line in note.get("notes", []):
        print(f"  — {line}")


if __name__ == "__main__":
    main()
