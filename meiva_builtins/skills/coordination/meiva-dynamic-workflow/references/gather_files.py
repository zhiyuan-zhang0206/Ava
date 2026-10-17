"""Reference watcher: wake the orchestrator at one checkpoint.

Workers finish silently — each writes its result file, and none
of them messages the orchestrator.  A checkpoint is a place the ORCHESTRATOR
script picked to be woken: this watcher polls `HANDOFF_DIR` until the
checkpoint condition holds, then delivers one wake message.  One-shot — it
exits after messaging.

The wake is sent once; the SDK's own bounded retries are the only retry. If
the send still fails the watcher logs the error and exits 2, so the launcher
gets an exit notice and re-launches; a wake that lands in a gateway / agent
restart window is not redelivered.

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
1. Read this file with meiva.files.read(...)
2. Replace HANDOFF_DIR, EXPECTED_FILES (or MATCH_GLOB), REQUIRED_COUNT,
   ORCHESTRATOR_ID
3. Launch with meiva.watcher.launch(code, timeout="10m", name="gather-<checkpoint>")
4. The watcher's message wakes you

Use a distinct run or input-version directory and preserve valid results on
re-entry. Existence is only a readiness hint: validate contents after waking.
Files remain observable when the watcher starts after successful dispatch;
there is no lost event to prevent by launching before an uncertain spawn.
"""

import time
from pathlib import Path

import meiva

# ── Configure before launching ───────────────────────────────────────────────
HANDOFF_DIR = ""  # e.g. "/home/.../task_handoff"
EXPECTED_FILES: list[str] = []  # e.g. ["flights.json", "hotels.json"]
MATCH_GLOB = ""  # e.g. "w5_feedback_*.json" — instead of naming the files
REQUIRED_COUNT = 0  # 0 = all of EXPECTED_FILES; K > 0 = wake at K of them
ORCHESTRATOR_ID = 0  # the agent to wake at this checkpoint

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
    """Deliver the checkpoint wake once; a failed send logs and exits 2."""
    try:
        meiva.agents.send_message(ORCHESTRATOR_ID, message)
    except Exception as exc:  # a lost wake must surface as an exit notice
        print(f"wake delivery failed: {exc!r}", flush=True)
        raise SystemExit(2) from None


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
