"""Per-agent turn-progress clock — the hosted runner's stall eye.

``agents_meta.last_active_at`` is the durable activity clock, but it is written
only on COMPLETED LLM steps and through a DB round trip; a turn blocked inside
one tool call, one LLM stream, or the runtime build shows no completed step
for minutes. This clock records the three most recent moments a turn showed
ANY activity — a LangGraph node enter, an LLM stream chunk, or a completed LLM
step — at in-process monotonic granularity, so the hosted daemon can ask "has
this turn done anything in the last N seconds?" without touching the DB.

The agent host builds one `TurnProgress` and holds it: it hands it to the turns it runs
(`AvaContext.turn_progress`, where the graph's writers mark activity) and to its own readers.
Three consumers read the clock:

- the host's stall guard aborts a ``graph.ainvoke`` whose turn clock has been silent for
  ``AVA_HOST_TURN_NO_PROGRESS_TIMEOUT_SECONDS`` (the turn-level timeout: a turn that keeps
  stepping may legitimately run for days, a turn that stops stepping is the failure this clock
  exists to bound);
- the dispatcher's durable scan treats an in-flight agent with a stale clock as turn-level
  fake-alive (process alive / turn dead — the exact shape the incident behind task #2417
  escaped through: agent 2998 claimed its whole inbound queue, then hung inside
  ``graph.ainvoke`` for 3.5h with no pending row ever aging) and cancels + reschedules it;
- the host daemon snapshots active turns onto its existing 15-second Redis heartbeat, so the
  gateway delivery watchdog can make the same liveness judgment outside the process whose event
  loop may freeze.

A turn queued at the host's admission gate shows no activity by design; the gate itself
(`TurnAdmission`) answers "queued" so the stall scan can tell it from "stuck".

One host process serves every local agent and per-agent monotonic timestamps need no
cross-process coordination. No locks: every writer runs on the host's single event loop
(LangGraph node tasks, the LLM node, the dispatcher loop), so a dict operation is atomic with
respect to every reader.
"""

from __future__ import annotations

import time
from typing import TypedDict


class TurnProgressSnapshot(TypedDict):
    """Serializable view of one active turn's recent monotonic marks."""

    age_s: float
    last_marks: list[float]


_MARK_HISTORY = 3


class TurnProgress:
    """agent_id -> the latest three activity timestamps (`time.monotonic()`).

    Entries are created at turn start and refreshed on activity; they are small and bounded by
    the number of agents this host has served, so no pruning is warranted: a stale entry for an
    idle agent is never read (consumers gate on in-flight).
    """

    def __init__(self) -> None:
        self._marks: dict[int, list[float]] = {}

    def mark(self, agent_id: int) -> None:
        """Record that agent ``agent_id``'s turn showed activity right now.

        Called from the graph's node lifecycle (every node enter), stream callback (every LLM
        chunk), and completed-LLM-step persist. The three-item append is intentionally cheap and
        contains no I/O, so it is safe on the hot path.
        """
        marks = self._marks.setdefault(agent_id, [])
        marks.append(time.monotonic())
        del marks[:-_MARK_HISTORY]

    def snapshot(self, agent_id: int) -> TurnProgressSnapshot | None:
        """Return age plus a copy of the latest three marks, or None if unknown."""
        marks = self._marks.get(agent_id)
        if not marks:
            return None
        return {
            "age_s": time.monotonic() - marks[-1],
            "last_marks": list(marks),
        }

    def age_s(self, agent_id: int) -> float | None:
        """Seconds since the agent's turn last showed activity, or None.

        ``None`` means the clock has no entry — no turn has marked progress on this host (or
        the host just started), which the callers treat as "not stale" so a fresh host never
        cancels turns it knows nothing about.
        """
        snapshot = self.snapshot(agent_id)
        return None if snapshot is None else snapshot["age_s"]

    def reset(self, agent_id: int) -> None:
        """Start a fresh progress window for ``agent_id`` (called at turn start).

        Without this, a long-idle agent's stale entry would be read as "stalled" the moment its
        next turn begins — the age would carry over from the previous turn instead of starting
        at zero.
        """
        self._marks[agent_id] = [time.monotonic()]
