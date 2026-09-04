"""In-process per-agent turn-progress clock — the hosted runner's stall eye.

``agents_meta.last_active_at`` is the durable activity clock, but it is written
only on COMPLETED LLM steps and through a DB round trip; a turn blocked inside
one tool call, one LLM stream, or the runtime build shows no completed step
for minutes. This registry records the three most recent moments a turn showed
ANY activity — a LangGraph node enter, an LLM stream chunk, or a completed LLM
step — at in-process monotonic granularity, so the hosted daemon can ask "has
this turn done anything in the last N seconds?" without touching the DB.

Three consumers read the clock:

- ``host.py``'s stall guard aborts a ``graph.ainvoke`` whose turn clock has
  been silent for ``AVA_HOST_TURN_NO_PROGRESS_TIMEOUT_SECONDS`` (the turn-level
  timeout: a turn that keeps stepping may legitimately run for days, a turn
  that stops stepping is the failure this clock exists to bound);
- ``dispatcher.py``'s durable scan treats an in-flight agent with a stale
  clock as turn-level fake-alive (process alive / turn dead — the exact shape
  the incident behind task #2417 escaped through: agent 2998 claimed its whole
  inbound queue, then hung inside ``graph.ainvoke`` for 3.5h with no pending
  row ever aging) and cancels + reschedules it.
- ``services/agent_host/daemon.py`` snapshots active turns onto its existing
  15-second Redis heartbeat. The gateway delivery watchdog can therefore make
  the same liveness judgment outside the process whose event loop may freeze.

Process mode does not use this registry: its wedged controller already owns
per-agent recovery over a pid, and a process-internal clock would be invisible
to the controller that lives in another daemon.

Module-level state on purpose. One host process serves every local agent, both
consumers are that process, and per-agent monotonic timestamps need no
cross-process coordination. No locks: every writer runs on the host's single
event loop (LangGraph node tasks, the LLM node, the dispatcher loop), so a
dict operation is atomic with respect to every reader.
"""

from __future__ import annotations

import time
from typing import TypedDict


class TurnProgressSnapshot(TypedDict):
    """Serializable view of one active turn's recent monotonic marks."""

    age_s: float
    last_marks: list[float]


_MARK_HISTORY = 3

# agent_id -> latest three activity timestamps (time.monotonic()). Entries are
# created at turn start and refreshed on activity; they are small and bounded by
# the number of agents this host has served, so no pruning is warranted — a
# stale entry for an idle agent is never read (consumers gate on in-flight).
_PROGRESS: dict[int, list[float]] = {}


def mark_turn_progress(agent_id: int) -> None:
    """Record that agent ``agent_id``''s turn showed activity right now.

    Called from the graph's node lifecycle (every node enter), stream callback
    (every LLM chunk), and completed-LLM-step persist. The three-item append is
    intentionally cheap and contains no I/O, so it is safe on the hot path.
    """
    marks = _PROGRESS.setdefault(agent_id, [])
    marks.append(time.monotonic())
    del marks[:-_MARK_HISTORY]


def turn_progress_snapshot(agent_id: int) -> TurnProgressSnapshot | None:
    """Return age plus a copy of the latest three marks, or None if unknown."""
    marks = _PROGRESS.get(agent_id)
    if not marks:
        return None
    return {
        "age_s": time.monotonic() - marks[-1],
        "last_marks": list(marks),
    }


def turn_progress_age_s(agent_id: int) -> float | None:
    """Seconds since the agent's turn last showed activity, or None.

    ``None`` means the clock has no entry — no turn has marked progress on
    this host (or the host just started), which the callers treat as "not
    stale" so a fresh host never cancels turns it knows nothing about.
    """
    snapshot = turn_progress_snapshot(agent_id)
    return None if snapshot is None else snapshot["age_s"]


def reset_turn_progress(agent_id: int) -> None:
    """Start a fresh progress window for ``agent_id`` (called at turn start).

    Without this, a long-idle agent's stale entry would be read as "stalled"
    the moment its next turn begins — the age would carry over from the
    previous turn instead of starting at zero.
    """
    _PROGRESS[agent_id] = [time.monotonic()]
