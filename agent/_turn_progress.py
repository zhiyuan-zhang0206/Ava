"""In-process per-agent turn-progress clock — the hosted runner's stall eye.

``agents_meta.last_active_at`` is the durable activity clock, but it is written
only on COMPLETED LLM steps and through a DB round trip; a turn blocked inside
one tool call, one LLM stream, or the runtime build shows no completed step
for minutes. This registry records the most recent moment a turn showed ANY
activity — a LangGraph node enter, a completed LLM step — at in-process
monotonic granularity, so the hosted daemon can ask "has this turn done
anything in the last N seconds?" without touching the DB.

Two consumers live inside the hosted daemon process:

- ``host.py``'s stall guard aborts a ``graph.ainvoke`` whose turn clock has
  been silent for ``AVA_HOST_TURN_NO_PROGRESS_TIMEOUT_SECONDS`` (the turn-level
  timeout: a turn that keeps stepping may legitimately run for days, a turn
  that stops stepping is the failure this clock exists to bound);
- ``dispatcher.py``'s durable scan treats an in-flight agent with a stale
  clock as turn-level fake-alive (process alive / turn dead — the exact shape
  the incident behind task #2417 escaped through: agent 2998 claimed its whole
  inbound queue, then hung inside ``graph.ainvoke`` for 3.5h with no pending
  row ever aging) and cancels + reschedules it.

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

# agent_id -> last activity (time.monotonic()). Entries are created at turn
# start and refreshed on activity; they are small and bounded by the number of
# agents this host has served, so no pruning is warranted — a stale entry for
# an idle agent is never read (consumers gate on the agent being in flight).
_PROGRESS: dict[int, float] = {}


def mark_turn_progress(agent_id: int) -> None:
    """Record that agent ``agent_id``''s turn showed activity right now.

    Called from the graph's node lifecycle (every node enter) and from the
    completed-LLM-step persist — cheap (a dict write, no I/O), so it is safe
    on the per-node hot path.
    """
    _PROGRESS[agent_id] = time.monotonic()


def turn_progress_age_s(agent_id: int) -> float | None:
    """Seconds since the agent's turn last showed activity, or None.

    ``None`` means the clock has no entry — no turn has marked progress on
    this host (or the host just started), which the callers treat as "not
    stale" so a fresh host never cancels turns it knows nothing about.
    """
    ts = _PROGRESS.get(agent_id)
    return None if ts is None else time.monotonic() - ts


def reset_turn_progress(agent_id: int) -> None:
    """Start a fresh progress window for ``agent_id`` (called at turn start).

    Without this, a long-idle agent's stale entry would be read as "stalled"
    the moment its next turn begins — the age would carry over from the
    previous turn instead of starting at zero.
    """
    mark_turn_progress(agent_id)
