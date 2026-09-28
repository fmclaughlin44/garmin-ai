"""
A single-process lock for anything that talks to Garmin or writes the store.

Three things can start a sync: the 06:30 launchd job, the two-hourly top-up, and
a person running refresh.command or backfill.py. Nothing stopped them
overlapping. Two processes hitting the Garmin API at once doubles the request
rate the 0.4s pacing was chosen to stay under, and two processes writing the same
store file race — the temp-file-and-rename in store.py makes each individual
write atomic, but it doesn't stop a slow backfill and a fast top-up from
interleaving their view of the world.

An advisory flock is enough here: every writer is a process on this machine, and
a stale lock dies with the process that held it.
"""

import errno
import fcntl
import os
import sys
from contextlib import contextmanager
from pathlib import Path

LOCK_PATH = Path(__file__).resolve().parent / ".tmp-runlock"


@contextmanager
def exclusive(name: str = "pipeline", wait: bool = False):
    """Hold the pipeline lock for the duration of the block.

    With wait=False (the default for scheduled jobs) a run that finds the lock
    held gives up immediately and says who has it. A scheduled job that can't run
    now will run in two hours; queueing them up behind a long backfill would just
    stack requests against the API.
    """
    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    handle = open(LOCK_PATH, "a+")
    flags = fcntl.LOCK_EX if wait else (fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        fcntl.flock(handle.fileno(), flags)
    except OSError as e:
        if e.errno not in (errno.EACCES, errno.EAGAIN):
            raise
        handle.seek(0)
        holder = handle.read().strip() or "another process"
        handle.close()
        print(f"Another pipeline run is in progress ({holder}). Skipping this one.",
              file=sys.stderr)
        raise SystemExit(0)

    handle.seek(0)
    handle.truncate()
    handle.write(f"{name} pid={os.getpid()}")
    handle.flush()
    try:
        yield
    finally:
        try:
            handle.seek(0)
            handle.truncate()
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()
