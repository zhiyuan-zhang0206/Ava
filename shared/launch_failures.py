"""The session names the last `ava start` on this host could not launch.

A session spawn failing is not a readiness question — the process was never
spawned, so no amount of waiting makes it appear. `_launch_sessions` retries
once and then reports; this module is how that report survives the process
boundary the rollout puts in the middle.

The rollout's local leg runs `ava start` in a fresh child
(`update._boot_gateway_fresh`) so start loads the just-synced revision. The
parent orchestration has to name the failed sessions in the ROLLOUT aftermath
block, and neither channel it already has can carry them: an exit code is one
integer, and computing the roster in the parent would read the PRE-pull tree's
service list (a rollout that retires a service would make the parent report a
session the new code no longer has). So the child — which holds the new roster
and did the launching — writes the names down.

`record` runs on every start, including the successful ones, so the file only
ever describes the most recent start; `take` reads and unlinks in one step, so a
consumed report cannot be counted twice.
"""

from __future__ import annotations

import json
from contextlib import suppress
from typing import cast

import shared.paths


def record(sessions: list[str]) -> None:
    """Write the sessions this start could not launch (empty clears the file).

    Unconditional at the end of every start's launch step: an absent file has to
    mean "the last start launched everything", not "no start ever wrote one",
    which it would if success left a previous run's names standing.

    Best-effort — a start must not fail because it could not write its own
    footnote. The write is atomic (temp + rename) so a reader never sees a
    partial list.
    """
    target = shared.paths.launch_failures_path()
    try:
        if not sessions:
            target.unlink(missing_ok=True)
            return
        tmp = target.with_suffix(".tmp")
        tmp.write_text(json.dumps(sessions), encoding="utf-8")
        tmp.replace(target)
    except OSError:  # fail-fast-ok: a diagnostic footnote must not fail the start
        pass


def take() -> list[str]:
    """Read the recorded session names and clear the file; `[]` when there are none.

    Read-and-unlink because the report belongs to exactly one consumer: the
    rollout leg that just ran the child. Leaving it would let the next rollout
    inherit a resolved failure and report it a second time.

    An unreadable or malformed file reads as `[]`, and a clear that fails is
    swallowed like the write is — the file is a footnote on a start that already
    printed its own crosses, and neither reading nor clearing it may abort a
    rollout. The cost of a failed clear is one duplicate report next time, which
    is strictly better than taking the rollout down over a stale footnote.
    """
    target = shared.paths.launch_failures_path()
    try:
        raw = target.read_text(encoding="utf-8")
    except OSError:
        return []
    finally:
        with suppress(OSError):  # fail-fast-ok: see the docstring
            target.unlink(missing_ok=True)
    try:
        parsed: object = json.loads(raw)
    except json.JSONDecodeError:
        return []
    if not isinstance(parsed, list):
        return []
    return [str(name) for name in cast(list[object], parsed)]
