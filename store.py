"""
Persistent, append-only storage for synced Garmin data.

Why this exists
---------------
The original pipeline wrote everything into a single ``garmin/data.json`` that
was rewritten from scratch on every sync. Because the sync only pulls a short
trailing window (10 days), each run silently *destroyed* everything older. That
made the interesting signals impossible to compute: acute-to-chronic workload
ratio needs 28 days of training load, and a resting-HR baseline needs a month
of mornings to be meaningful.

The store fixes that by keeping one small JSON file per day and per activity:

    garmin/store/days/YYYY-MM-DD.json      one day of wellness payloads
    garmin/store/activities/<id>.json      one activity, keyed by Garmin's id

A sync overwrites only the days it actually fetched (fresh data wins, because
Garmin backfills sleep and stress for a day or two) and never touches anything
outside its window. Activities are keyed by id, so re-pulling the same window
is idempotent rather than duplicative.

One file per record is deliberate: it is diff-friendly, it makes a partial
failure lose one day instead of the whole history, and it means the writer never
has to read the entire history into memory to append to it.
"""

import json
from datetime import date, datetime, timedelta
from pathlib import Path

BASE = Path(__file__).resolve().parent
STORE = BASE / "garmin" / "store"
DAYS_DIR = STORE / "days"
ACTIVITIES_DIR = STORE / "activities"


def ensure_dirs() -> None:
    DAYS_DIR.mkdir(parents=True, exist_ok=True)
    ACTIVITIES_DIR.mkdir(parents=True, exist_ok=True)


def _read_json(path: Path):
    try:
        return json.loads(path.read_text())
    except Exception:
        return None


def _write_json(path: Path, payload) -> None:
    """Write via a temp file + rename so a crash can't leave a truncated file."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, default=str))
    tmp.replace(path)


# --------------------------------------------------------------------------
# Days
# --------------------------------------------------------------------------

def put_day(day_str: str, payload: dict) -> None:
    """Store one day of wellness payloads, replacing any earlier copy.

    Replacing rather than merging is intentional: Garmin revises a day's sleep
    and stress numbers for a day or two after the fact, so the newest pull is
    the most accurate one. We only drop a fresh payload if it is *empty* and we
    already hold real data, which is what a failed API call looks like.
    """
    ensure_dirs()
    path = DAYS_DIR / f"{day_str}.json"
    if path.exists() and not _has_content(payload):
        existing = _read_json(path)
        if existing is not None and _has_content(existing):
            return  # don't let a failed pull erase a good day
    _write_json(path, payload)


def _has_content(payload: dict) -> bool:
    if not isinstance(payload, dict):
        return False
    for key in ("stats", "sleep", "hrv", "body_battery", "stress", "steps", "training_readiness"):
        value = payload.get(key)
        if value not in (None, {}, []):
            return True
    return False


def get_day(day_str: str):
    return _read_json(DAYS_DIR / f"{day_str}.json")


def all_days() -> dict:
    """Every stored day, as {'YYYY-MM-DD': payload}, ascending by date."""
    ensure_dirs()
    out = {}
    for path in sorted(DAYS_DIR.glob("*.json")):
        payload = _read_json(path)
        if payload is not None:
            out[path.stem] = payload
    return out


def days_in_range(start: date, end: date) -> dict:
    """Stored days with start <= day <= end (inclusive), ascending."""
    wanted = set()
    cur = start
    while cur <= end:
        wanted.add(cur.isoformat())
        cur += timedelta(days=1)
    return {k: v for k, v in all_days().items() if k in wanted}


# --------------------------------------------------------------------------
# Activities
# --------------------------------------------------------------------------

def put_activity(activity: dict) -> None:
    """Store one activity keyed by Garmin's activityId (idempotent)."""
    ensure_dirs()
    activity_id = activity.get("activityId")
    if activity_id is None:
        return
    _write_json(ACTIVITIES_DIR / f"{activity_id}.json", activity)


def all_activities() -> list:
    """Every stored activity, ascending by local start time."""
    ensure_dirs()
    out = []
    for path in sorted(ACTIVITIES_DIR.glob("*.json")):
        payload = _read_json(path)
        if isinstance(payload, dict):
            out.append(payload)
    out.sort(key=lambda a: a.get("startTimeLocal") or "")
    return out


def activity_date(activity: dict):
    """The local calendar date an activity started on, as a date, or None."""
    start = activity.get("startTimeLocal") or ""
    if not start:
        return None
    try:
        return datetime.fromisoformat(str(start).replace("T", " ")).date()
    except Exception:
        try:
            return datetime.strptime(str(start)[:10], "%Y-%m-%d").date()
        except Exception:
            return None


def coverage() -> dict:
    """A quick summary of what the store holds -- used by the CLI and README."""
    days = all_days()
    acts = all_activities()
    day_keys = sorted(days.keys())
    act_dates = [activity_date(a) for a in acts]
    act_dates = sorted(d for d in act_dates if d)
    return {
        "day_count": len(day_keys),
        "first_day": day_keys[0] if day_keys else None,
        "last_day": day_keys[-1] if day_keys else None,
        "activity_count": len(acts),
        "first_activity": act_dates[0].isoformat() if act_dates else None,
        "last_activity": act_dates[-1].isoformat() if act_dates else None,
    }


if __name__ == "__main__":
    import pprint
    pprint.pprint(coverage())
