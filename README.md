# Endurance training dashboard

A nightly pipeline that pulls my Garmin Connect data, computes the training and
recovery signals the watch doesn't give me, and renders a single self-contained
dashboard for an 8-week marathon block.

`demo/index.html` — the same dashboard on synthetic data · Python 3.12 · no framework, no build step

---

## What it does

Garmin Connect will show you yesterday. It won't tell you whether your training
load is ramping faster than your body is absorbing it, whether this morning's
resting heart rate is meaningful or just noise, or whether your recent running
actually projects to the time you're chasing. Those answers need history and
arithmetic, so this pipeline keeps the history and does the arithmetic.

Every night it:

1. pulls wellness and activity data from Garmin Connect into an append-only store
2. computes the derived signals — training load, acute:chronic workload ratio,
   fitness/fatigue/form, resting-HR and nocturnal-drop baselines, sleep debt, a
   readiness composite, a race projection, and adherence against the plan
3. matches what actually happened against the plan, and adjusts today and
   tomorrow to the recovery signals — without ever rewriting the base plan
4. writes a coach's read of those signals
5. renders `dashboard.html`, which opens with a double-click and needs no server

## Architecture

```mermaid
flowchart LR
  G[Garmin Connect API] -->|sync_garmin.py| S[(garmin/store/<br/>one JSON per day<br/>one per activity)]
  P[plan/build_plan.py] -->|generates| PJ[plan/plan.json]
  S --> SIG[signals.py]
  PJ --> SIG
  SIG --> D[(web/data.json<br/>the output contract)]
  D --> A[adapt.py<br/>match + adjust today]
  A --> D
  D --> C[coach.py<br/>rules engine · optional Claude]
  C --> D
  D --> B[build_dashboard.py]
  T[web/template.html] --> B
  B --> H[dashboard.html]
  D -.->|synthetic variant| DEMO[demo/index.html]
```

Each stage reads the previous stage's output from disk and nothing else, so any
stage can be run or debugged on its own.

| File | Job |
|---|---|
| `login.py` | one-time Garmin auth, including the MFA handshake; stores a reusable token |
| `sync_garmin.py` | Garmin API → `garmin/store/` plus readable markdown notes |
| `store.py` | the append-only store: one JSON file per day, one per activity |
| `signals.py` | **the analytical core.** Store + plan → `web/data.json` |
| `adapt.py` | matches activities to the plan, and adjusts today and tomorrow |
| `coach.py` | reads the signals, writes the coach's note |
| `build_dashboard.py` | inlines `web/data.json` into `web/template.html` |
| `plan/build_plan.py` | generates the 8-week plan from a single goal time |
| `make_demo_data.py` | synthetic dataset so the project can be shown publicly |
| `run_daily.py` | runs the whole pipeline; called by a launchd job at 06:30 |
| `tests/test_signals.py` | tests over the signal layer |
| `tests/test_adapt.py` | tests over matching and adaptation |

## The decisions worth explaining

**The signals are computed in Python, not in the browser.** The interesting
numbers need history: an acute:chronic workload ratio reads 28 days of training
load, and a resting-HR baseline is meaningless against fewer than a few weeks of
mornings. Shipping a month of raw Garmin payloads to the page to recompute that
on every load would be absurd. Computing it once a night also makes it stable,
auditable and testable — `signals.py` is pure functions over plain data.

**The store is append-only, and that was a bug fix.** The first version of this
project wrote everything into a single `garmin/data.json`, rebuilt from scratch
on each run. Because the sync only pulls a trailing window, every run silently
destroyed everything older — which made the 28-day signals impossible to compute
without anyone noticing, since the dashboard rendered perfectly from 10 days of
data. The store now keeps one small file per day and per activity, and a sync
replaces only the days it actually fetched. `migrate_store.py` recovered the lost
history by parsing the human-readable markdown notes the old pipeline had written
alongside the snapshot and never deleted.

**There is no HRV on this watch, so the nocturnal heart-rate drop stands in for
it.** Probed directly: `get_hrv_data` and `get_hrv_data_range` both return `{}`,
and `get_training_readiness`, `get_morning_training_readiness` and
`get_endurance_score` are empty too. There is no sleep score either. All of them
come back *empty rather than errored*, which is exactly how they become a chart of
zeros if nobody checks.

What the sleep response does carry — and the first version of this pipeline was
throwing away — is a full overnight series: heart rate roughly every two minutes,
plus overnight stress and body battery. From that comes the **nocturnal drop**:
heart rate at sleep onset minus the night's minimum. A deep drop is
parasympathetic rebound; a shallow one is a body still working. It is not HRV and
is never labelled as HRV, but it moves for the same reasons, and on real data it
is more sensitive than resting HR alone — the night resting HR was 3 bpm high, the
drop had collapsed from a usual 8 bpm to 0.8 and bottomed out immediately.

Readiness is therefore a weighted composite of resting-HR deviation, the nocturnal
drop against its own baseline, sleep duration and stage composition, body battery
at wake, stress, and computed training load. Missing inputs are dropped and the
remaining weights renormalised, so a partially synced day scores on its available
signals instead of being punished for the gaps — and every score carries the list
of inputs behind it, because a score built from two signals deserves less trust
than one built from five. The dashboard names both what is in use and what is
unavailable.

**Two load models, because they answer different questions.** The acute:chronic
workload ratio asks "am I ramping faster than I can absorb" — an injury-risk
question. Fitness/fatigue/form (42-day and 7-day exponentially weighted load, and
their difference) asks "am I fit, and am I fresh enough to express it" — a peaking
question. A marathon block needs both: the ratio keeps you healthy through the
build, form tells you whether the taper is working.

**Form is judged as a share of fitness, not as an absolute number.** The published
form thresholds (+5 to +15 "fresh", −10 to −30 "building") come from TSS, where
fitness typically lands between 50 and 100. This pipeline measures load in TRIMP,
which has a different scale, so borrowing those cutoffs unchanged mislabelled a
normal training week as a crisis. As a percentage of fitness the bands hold
whatever the unit is.

**The plan adapts, but never rewrites itself.** `plan/plan.json` is generated once
from the goal time and then left alone. Each build adjusts only today and
tomorrow, and the dashboard shows the suggestion *next to* the original session so
I pick which one to do. A plan that quietly rewrites itself every morning is a
plan you stop trusting. Triggers: readiness ≥ 70 takes it as written; 45–69 turns
quality into easy running at the same distance and trims the long run 10%; below
45 is rest or 30 minutes very easy, with any key session moved to a later easy day
— never the day before another key session; three of the last four days below 55
cuts the rest of the week 20%; a fatigue/fitness ratio above 1.4 eases the next
workout; two or more key sessions missed last week holds this week at last week's
volume. Lifting scales down with recovery rather than being dropped.

**Completion is measured, not ticked.** A run on the planned day covering at least
60% of the planned distance counts as done; less is cut short; a run on another
day fills the nearest unmatched run that week, because shifting a session by a day
is normal life and shouldn't read as a failure. A session planned for today is
neither done nor missed — a dashboard that scolds you at breakfast for a run
you're about to do gets ignored within a week.

**Garmin's own numbers are kept alongside mine, precisely because they disagree.**
`get_race_predictions` and `get_max_metrics` both work on this account even though
the readiness endpoints don't. Garmin models a marathon time from every run on the
watch and reads optimistic; Riegel extrapolates one training run and reads
pessimistic. On the same day they said 3:50:15 and 4:02:45. Showing the bracket is
more honest than picking whichever number flatters the goal. These endpoints return
only the current value with no history, so each observation is stored and the trend
is the store's.

**Training load is TRIMP, not duration.** Banister's formula
(`minutes × HRr × 0.64 × e^(1.92 × HRr)`) weights hard minutes far more heavily
than easy ones, so an hour of intervals and an hour of jogging don't score alike.
Sessions without average HR fall back to a load-per-minute rate *measured from my
own sessions in that sport* where there are at least three to measure from, and a
generic prior otherwise — either way flagged as estimated.

**The load window starts where the data starts.** It used to extend a fixed 120
days back, which padded the months before the watch existed with zeros. Since the
fitness curve is seeded from the earliest weeks, it seeded from nothing and spent
months climbing to a fitness I already had — understating fitness and so
overstating fatigue across the whole series. Fitness read 51 with that bug and 79
without it, which is the difference between "building" and "fresh" two weeks after
a race.

**The sync is incremental and stops on a 429.** Calls are spaced 0.4s apart. Days
already held are skipped except the most recent three, because Garmin keeps
revising a day's sleep and stress for a day or two after it ends and everything
older is settled. A rate-limit response aborts the run rather than retrying in a
loop, and an old day Garmin has nothing for is cached as failed so it isn't
retried forever.

**The race projection reports three numbers, not one.** Riegel's formula assumes
a maximal effort, and a training run never quite is. So the projection gives the
read from the fastest-projecting run (closest to maximal), from the longest run
(where Riegel extrapolates most reliably), and from the slowest, to show the
spread. It's labelled a floor rather than a forecast.

**How alarming a pace gap is depends on the calendar.** A 20-minute gap seven
weeks out is the normal starting position for a block; the same gap in race week
is the story of the day. The coach's severity scales with days to race rather
than with the gap alone.

**The data is inlined into the page rather than fetched.** A page opened as a
local file can't `fetch()` a sibling JSON file — the browser blocks it as
cross-origin. Since the whole point is double-clicking `dashboard.html` and
seeing today's numbers, the data ships inside the page. The separation is still
real: swap `web/data.json` and you get a different dashboard, which is exactly
how the demo build works.

**Calorie targets are built from measured expenditure, not an estimated TDEE.**
Garmin's daily summary reports `bmrKilocalories` and `activeKilocalories`, and
their sum is measured total daily energy expenditure — which beats a
Mifflin-St Jeor estimate that would need height and would ignore how much I
actually moved. One catch that took a test to pin down: the current day's BMR is
only accrued to the present moment, so today's total is always an undercount and
has to be excluded. A day counts as complete when its BMR is at least 90% of the
window's typical BMR.

Macros are then assigned in a fixed order, and the periodisation falls out of it
rather than needing its own rule:

1. **Protein** is flat at 1g per lb of bodyweight whatever the day, because the
   point of it is preserving muscle in a deficit.
2. **Fat** gets a floor in g/lb that varies by day type, so it never drops to a
   level that costs you hormonally just to make room for carbohydrate.
3. **Carbohydrate** takes every calorie left.

Because hard days have a bigger calorie budget *and* a lower fat floor,
carbohydrate absorbs the whole difference automatically. A long-run day lands at
maintenance with carbs above 50% of calories and fat near 20%; a rest day runs a
450kcal deficit with carbs cut roughly in half and fat at its highest. Intake is
still not tracked — Garmin carries no food log — so these are targets, never a
record of what was eaten.

**Everything is on one page.** No tabs, no accordions, no drawers. This week's
plan is the first thing on screen because it is the thing I actually open the page
to see; everything else is a scroll away rather than a click away. A dashboard you
have to navigate is a dashboard you check less often.

**The coach's read is a rules engine by default.** It's deterministic, free,
works offline, and can be unit-tested. `coach.py --live` instead hands the
*computed signals* — never raw payloads — to Claude and asks for the read in
plain language, falling back to the rules engine if the key is missing or the
call fails. The model never does arithmetic the pipeline has already done.

## Running it

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt

.venv/bin/python login.py          # one-time Garmin auth (handles MFA)
.venv/bin/python plan/build_plan.py
.venv/bin/python run_daily.py      # sync → signals → coach → dashboard
open dashboard.html
```

Individual stages:

```bash
.venv/bin/python sync_garmin.py --days 10       # incremental; --force to re-pull
.venv/bin/python signals.py --print
.venv/bin/python adapt.py                       # matching + today's adjustments
.venv/bin/python coach.py --live                # needs ANTHROPIC_API_KEY
.venv/bin/python run_daily.py --skip-sync       # rebuild without hitting Garmin
.venv/bin/pytest tests/ -q
```

`config.json` holds the few things that can't be derived from Garmin data
(bodyweight for carb targets, an HRmax override, the sleep target, the readiness
weights). Anything left `null` is either estimated from my own history or
reported as unavailable — nothing is silently guessed.

### Automation

Scheduled every morning at 06:30 via launchd (`com.garminai.dailysync`), which
runs the whole pipeline so `dashboard.html` is current before I look at it. If the
Mac is asleep at 06:30, launchd runs the job at the next wake. `refresh.command`
is double-clickable for an on-demand run, and logs rotate at 512KB so they don't
grow without bound.

A failed Garmin pull doesn't take the run down: the store still holds yesterday,
the remaining stages rebuild from it, and the page states which day the data runs
through.

```bash
launchctl list | grep garminai                          # check it's loaded
launchctl kickstart -k gui/$(id -u)/com.garminai.dailysync   # force a run
tail -40 logs/daily_sync.log
```

The one thing the schedule can't do is refresh the phone copy. The dashboard is
also published as a private claude.ai artifact for reading on a phone, and
publishing needs an interactive session — a headless run has no way to do it. So
the local file is always current and the phone copy is a snapshot that gets
republished on request.

## Demo

`demo/index.html` is the same dashboard against a synthetic dataset from
`make_demo_data.py`. The repository is public; my resting heart rate, sleep and
location-tagged workouts are not. The real store is gitignored, and so is
`dashboard.html`, because the build inlines the real data into the page.

The demo athlete is fabricated but shaped like real Garmin output — including the
absent HRV, kept absent so the demo shows the readiness model this project
actually ships rather than an idealised one. Seeded, so it rebuilds identically.

Every synthetic build carries `"demo": true` in its payload, which puts a warning
banner at the very top of the page and prefixes the browser title with `DEMO`. That
banner exists because it was learned the hard way: a footer note two screens below
the charts was read straight past, and a fabricated VO₂ max and training week were
briefly taken for real ones. A demo that can be mistaken for real data is worse
than no demo.

## Tests

71 tests over `signals.py`, `adapt.py` and the nutrition layer, covering the edge
cases that would otherwise fail quietly rather than loudly:

- a workload ratio computed from 5 days of history would look plausible and mean
  nothing, so it refuses to produce one under 21 days
- a missing readiness input must not read as a zero input
- a median baseline has to survive one sensor artifact
- an HR reading below resting must not produce negative training load
- rest days are real data, so the daily-load series is dense rather than sparse
- an empty store has to render, not crash
- a session planned for today is neither done nor missed
- a run on the wrong day fills the nearest miss, but a run that already satisfied
  its own session is not reused for another
- a rescheduled key session never lands the day before another key session
- adaptation never mutates the base plan, and always keeps the original beside
  the suggestion
- today's partial BMR is excluded from maintenance, or the calorie target would
  fall every morning
- carbohydrate rises and fat falls monotonically with the day's demand, so the
  periodisation is verified rather than just described
- macros add back up to the calorie target, and carbs never go negative

```bash
.venv/bin/pytest tests/ -q
```

## What's deliberately not here

- **Strava.** Garmin is the source of truth for this data; adding Strava would
  mean a second OAuth flow and a deduplication problem for no new signal.
- **Body composition.** `get_body_composition` and `get_weigh_ins` return empty
  lists on this account — there's no connected scale — so bodyweight stays a
  config value and the nutrition panel says so rather than inventing a number.
- **Fitness age.** Garmin reports it, but it's a soft estimate that informs no
  decision, and putting it on screen just invites arguing with it.
- **Race-day and carb-loading guidance.** Dropped: it was static text that didn't
  come from any measurement, which is exactly the kind of filler this dashboard
  should not carry.
- **Disposable data.** Tempting to treat the raw pull as a rebuildable cache, but
  Garmin's history only goes back to when the watch does, and this project already
  lost a month of history to exactly that assumption. The store is durable.
- **A database.** 45 days of JSON files is a few hundred kilobytes. SQLite would
  be ceremony.
- **A frontend framework.** The page renders a fixed set of charts from one JSON
  object. React would be more code, not less.
