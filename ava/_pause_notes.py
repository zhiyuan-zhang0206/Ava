"""In-memory heartbeat-pause reminder buffer, delivered as a system note.

`ava.self.pause_heartbeat` records a reminder here when the new pause window
repeats or shortens the previous one (exponential-backoff violation, user
ruling 2026-08-29). The exec child drains the buffer into the result
envelope (`agent/exec_child.py`) and the exec node merges each entry into
the same exec's messages delta as a HEARTBEAT_PAUSE system note — the same
in-memory delivery design as `ava.security` findings (user ruling
2026-08-11: no side-channel files; the wrapper modifies the exec's
state-update messages key in memory).

The note lands AFTER the exec-result ToolMessage on purpose: the
Anthropic-compat wire contract requires an AIMessage's tool_use to be
immediately followed by its tool_result, so a note sandwiched between the
AIMessage and the ToolMessage is rejected with a 400 (same invariant
documented in `agent/graph/_exec_notes.py`).

Execs are serial per agent process, so a module-level list is race-free in
practice; the drain happens before anything else can append.
"""

from pydantic import BaseModel, ConfigDict


class PauseNote(BaseModel):
    """One exponential-backoff reminder buffered during an exec turn."""

    model_config = ConfigDict(frozen=True)

    content: str


_pending_pause_notes: list[PauseNote] = []


def record_pause_note(content: str) -> None:
    """Buffer one backoff reminder for delivery by the exec node as a system
    note. No-op outside an exec turn (no messages delta exists to inject
    into — same guard rationale as `ava.security`)."""
    import ava  # lazy: same-layer, avoids import cycle at module load

    if ava.state is None:
        return
    _pending_pause_notes.append(PauseNote(content=content))


def take_pause_notes() -> list[PauseNote]:
    """Return all pending reminders and clear the buffer.

    Called at the end of an exec child to carry each reminder through the
    result envelope. Returns an empty list when nothing was flagged.
    Clearing on read means each reminder is delivered exactly once.
    """
    global _pending_pause_notes  # noqa: PLW0603 — drain-and-reset is the contract
    out = _pending_pause_notes
    _pending_pause_notes = []
    return out
