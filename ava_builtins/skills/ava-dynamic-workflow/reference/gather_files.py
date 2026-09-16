"""Reference watcher: wake the orchestrator at one checkpoint.

Workers finish silently — each writes its result file, and none
of them messages the orchestrator.  A checkpoint is a place the ORCHESTRATOR
script picked to be woken: this watcher polls `HANDOFF_DIR` until the
checkpoint condition holds, then delivers one wake message.  One-shot — it
exits after messaging.

Delivery survives a restart window: the wake send retries with doubling gaps
(10s to a 160s cap, ~10.5 min in total) because a gateway / agent restart
window (an update wave, `ava cluster update`) outlasts the SDK's own 3 quick
retries; if every attempt fails the watcher exits 2.

The condition comes from the placeholders below:

- `EXPECTED_FILES` + `REQUIRED_COUNT = 0` — wake when every named file exists
  (the whole wave gates the checkpoint).
- `EXPECTED_FILES` + `REQUIRED_COUNT = K` — wake when any K of the named files
  exist (K-of-N: the rest of the fan-out keeps running while you reduce).
- `MATCH_GLOB` + `REQUIRED_COUNT = K` — wake at K files matching the glob, for
  when the result names are not all known at the time the checkpoint is armed.

Listing only SOME of the running workers in `EXPECTED_FILES` is the
designated-reporter pattern: ten workers run, the two whose output the next
step actually needs are named here, the other eight just end.

Usage:
1. Read this file with ava.files.read(...)
2. Replace HANDOFF_DIR, EXPECTED_FILES (or MATCH_GLOB), REQUIRED_COUNT,
   ORCHESTRATOR_ID
3. Launch with ava.watcher.launch(code, timeout="10m", name="gather-<checkpoint>")
4. The watcher's message wakes you

Launch it BEFORE spawning the workers so no result file is missed, and delete
the previous wave's files first: a stale file counts as landed.
"""

import time
from pathlib import Path

import ava

# ── Configure before launching ───────────────────────────────────────────────
HANDOFF_DIR = ""  # e.g. "/home/.../task_handoff"
EXPECTED_FILES: list[str] = []  # e.g. ["flights.json", "hotels.json"]
MATCH_GLOB = ""  # e.g. "w5_feedback_*.json" — instead of naming the files
REQUIRED_COUNT = 0  # 0 = all of EXPECTED_FILES; K > 0 = wake at K of them
ORCHESTRATOR_ID = 0  # the agent to wake at this checkpoint
WAKE_ATTEMPTS = 8  # wake delivery tries (first + 7 retries); the gaps below
# sum to ~10.5 min — long enough to ride out a gateway / agent restart window
WAKE_BACKOFF_S = 10.0  # first gap between wake tries; doubles per retry
WAKE_BACKOFF_MAX_S = 160.0  # cap for one gap

_dir = Path(HANDOFF_DIR)


def landed() -> list[str]:
    """Result files present so far."""
    if MATCH_GLOB:
        return sorted(p.name for p in _dir.glob(MATCH_GLOB))
    return [f for f in EXPECTED_FILES if (_dir / f).exists()]


def threshold() -> int:
    """How many files this checkpoint waits for."""
    if REQUIRED_COUNT > 0:
        return REQUIRED_COUNT
    if MATCH_GLOB:
        raise ValueError("MATCH_GLOB needs an explicit REQUIRED_COUNT — 'all' is undefined")
    if not EXPECTED_FILES:
        raise ValueError("configure EXPECTED_FILES, or MATCH_GLOB with REQUIRED_COUNT")
    return len(EXPECTED_FILES)


def _wake(message: str) -> None:
    """Deliver the checkpoint wake, retrying across a restart window.

    Delivery retries with growing gaps; if every attempt fails the watcher
    exits 2, so a lost wake surfaces as an exit notice instead of nothing.
    """
    delay = WAKE_BACKOFF_S
    for attempt in range(1, WAKE_ATTEMPTS + 1):
        try:
            ava.agents.send_message(ORCHESTRATOR_ID, message)
            return
        except Exception as exc:  # any transport failure retries
            print(f"wake attempt {attempt}/{WAKE_ATTEMPTS} failed: {exc!r}", flush=True)
            if attempt < WAKE_ATTEMPTS:
                time.sleep(delay)
                delay = min(delay * 2, WAKE_BACKOFF_MAX_S)
    print(f"wake delivery failed after {WAKE_ATTEMPTS} attempts", flush=True)
    raise SystemExit(2)


def watch(interval_s: float = 3.0) -> None:
    need = threshold()
    while True:
        ready = landed()
        if len(ready) >= need:
            message = f"checkpoint reached in {HANDOFF_DIR} ({len(ready)}/{need}): " + ", ".join(
                ready
            )
            _wake(message)
            return
        time.sleep(interval_s)


if __name__ == "__main__":
    watch()
