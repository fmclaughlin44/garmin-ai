"""
Checks whether the readiness model predicts anything.

Every curve in the readiness score is a hand-picked constant — `(10 - soreness) / 8`,
`1 - (delta + 2) / 7`, and so on. They are documented and defensible on
physiological grounds, but "defensible" is not "validated", and until there was
enough history it could not be tested at all. There is now 277 days.

The question this asks is narrow and answerable: does a day's readiness score
carry any information about what happened next? Three outcomes, each a thing the
score is implicitly claiming to predict:

  completion   did the planned session get done the next day
  volume       how far was actually run the next day
  efficiency   speed per heartbeat on the next run, i.e. did it go well

Correlation is Spearman's rank, which asks only whether the two move together in
rank order — the right question for a score whose absolute values are arbitrary.

This is an analysis tool, not part of the pipeline. Nothing on the dashboard
depends on it.

Usage:
  python3 analyse_readiness.py
"""

import json
import math
import statistics
from collections import defaultdict
from datetime import date, datetime, timedelta

import adapt
import signals
import store


def spearman(xs, ys):
    """Rank correlation, with average ranks for ties."""
    pairs = [(x, y) for x, y in zip(xs, ys) if x is not None and y is not None]
    if len(pairs) < 8:
        return None, len(pairs)

    def ranks(values):
        order = sorted(range(len(values)), key=lambda i: values[i])
        out = [0.0] * len(values)
        i = 0
        while i < len(order):
            j = i
            while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
                j += 1
            average = (i + j) / 2 + 1
            for k in range(i, j + 1):
                out[order[k]] = average
            i = j + 1
        return out

    xr, yr = ranks([p[0] for p in pairs]), ranks([p[1] for p in pairs])
    n = len(pairs)
    mx, my = sum(xr) / n, sum(yr) / n
    num = sum((a - mx) * (b - my) for a, b in zip(xr, yr))
    den = math.sqrt(sum((a - mx) ** 2 for a in xr) * sum((b - my) ** 2 for b in yr))
    return (num / den if den else None), n


def describe(rho):
    if rho is None:
        return "not enough paired days"
    strength = abs(rho)
    word = ("no relationship" if strength < 0.1 else
            "very weak" if strength < 0.2 else
            "weak" if strength < 0.35 else
            "moderate" if strength < 0.5 else "strong")
    return f"{rho:+.2f}  ({word})"


def main():
    days = store.all_days()
    activities = store.all_activities()
    manual = store.all_manual()
    plan = json.loads((signals.PLAN_JSON).read_text())
    config = json.loads((signals.CONFIG_JSON).read_text())
    as_of = date.today()

    history = [signals.extract_day_metrics(d, days[d], manual.get(d)) for d in sorted(days)]
    weights = config.get("readiness_weights")
    hr_max = signals.estimate_hr_max(activities, config.get("hr_max"))["value"]
    rhr_base = signals.rhr_baseline(history, history[-1]["date"]).get("baseline") or 50
    rates = signals.sport_load_rates(activities, hr_max, rhr_base)
    first = datetime.fromisoformat(history[0]["date"]).date()
    loads = signals.daily_loads(activities, hr_max, rhr_base, first, as_of, rates["rates"])
    series = signals.readiness_series(history, loads, weights,
                                      config.get("sleep_target_hours", 7.5))

    # --- next-day outcomes ---
    runs = [signals.run_summary(a) for a in activities if signals.is_run(a)]
    miles_by_date, eff_by_date = defaultdict(float), {}
    for r in runs:
        miles_by_date[r["date"]] += r["miles"]
        if r["efficiency_index"]:
            eff_by_date[r["date"]] = r["efficiency_index"]

    statuses = adapt.match_plan(plan.get("days", {}),
                               [{"date": r["date"], "miles": r["miles"]} for r in runs], as_of)

    scores, completion, volume, efficiency = [], [], [], []
    for entry in history:
        score = (series.get(entry["date"]) or {}).get("score")
        if score is None:
            continue
        tomorrow = (datetime.fromisoformat(entry["date"]).date() + timedelta(days=1)).isoformat()
        status = (statuses.get(tomorrow) or {}).get("status")
        scores.append(score)
        completion.append({"done": 1, "moved": 1, "short": 0.5}.get(status)
                          if status in ("done", "moved", "short", "missed")
                          else (0 if status == "missed" else None))
        volume.append(miles_by_date.get(tomorrow))
        efficiency.append(eff_by_date.get(tomorrow))

    print(f"Readiness model validation — {len(history)} days "
          f"({history[0]['date']} to {history[-1]['date']})")
    print(f"Scored days: {len(scores)}\n")

    print("Does today's readiness predict tomorrow?")
    for label, outcome in (("session completed", completion),
                           ("miles run", volume),
                           ("efficiency (speed per heartbeat)", efficiency)):
        rho, n = spearman(scores, outcome)
        print(f"  {label:34s} {describe(rho):28s} n={n}")

    # --- how much does each component actually move the score? ---
    print("\nComponent behaviour across the history:")
    print("  (a component that never varies contributes nothing, whatever its weight)")
    per_component = defaultdict(list)
    for entry in history:
        rhr = signals.rhr_baseline(history, entry["date"])
        noct = signals.nocturnal_baseline(history, entry["date"])
        sleep = signals.sleep_summary(history, entry["date"],
                                      config.get("sleep_target_hours", 7.5))
        load = signals.acwr(loads, datetime.fromisoformat(entry["date"]).date())
        result = signals.readiness(rhr, sleep, entry, load, weights, nocturnal=noct)
        for name, value in (result.get("components") or {}).items():
            if value is not None:
                per_component[name].append(value)

    for name in sorted(per_component):
        values = per_component[name]
        coverage = 100 * len(values) / len(history)
        spread = statistics.pstdev(values) if len(values) > 1 else 0
        weight = (weights or {}).get(name, 0)
        flag = ""
        if coverage < 40:
            flag = "  <- rarely available"
        elif spread < 0.05:
            flag = "  <- almost constant, so its weight does nothing"
        recent = [e for e in history[-60:]]
        recent_present = 0
        for e in recent:
            rhr_r = signals.rhr_baseline(history, e["date"])
            noct_r = signals.nocturnal_baseline(history, e["date"])
            sleep_r = signals.sleep_summary(history, e["date"],
                                            config.get("sleep_target_hours", 7.5))
            load_r = signals.acwr(loads, datetime.fromisoformat(e["date"]).date())
            comps = signals.readiness(rhr_r, sleep_r, e, load_r, weights,
                                      nocturnal=noct_r).get("components") or {}
            if comps.get(name) is not None:
                recent_present += 1
        recent_pct = 100 * recent_present / max(len(recent), 1)
        print(f"  {name:15s} weight {weight:>3}  present {coverage:5.1f}% all-time / "
              f"{recent_pct:5.1f}% last 60d  spread {spread:.3f}{flag}")

    print("\nRead this as a sanity check, not a proof. Training outcomes are")
    print("confounded by the plan itself: a scheduled rest day reads as a missed")
    print("session, and a taper week reads as lost volume.")


if __name__ == "__main__":
    main()
