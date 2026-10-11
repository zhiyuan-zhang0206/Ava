"""ava_memory plugin state: the memory notes the current exec call wrote.

The `files.write` / `append` / `edit` wraps (`plugin.py`) record each memory note a call wrote,
inside the exec subprocess; the after_exec hook (`agent_runtime.py`) reads the record in the agent
process, checks those notes and clears it. The record is a set of paths relative to the root they
live under, one set per root, merged by union so several execute_code calls of one assistant turn
add to the same record.
"""

from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, Field


def _set_union(current: set[str], new: set[str]) -> set[str]:
    return current | new


class MemoryWriteState(BaseModel):
    """plugins.ava_memory persistent state.

    - shared_notes: shared-pool notes (`ava.memory.PATH`) written and not yet checked,
      relative to the pool root, as posix paths.
    - personal_notes: the same for the agent's own notes (`ava.memory.PERSONAL_PATH`).
    """

    shared_notes: Annotated[set[str], _set_union] = Field(default_factory=set)
    personal_notes: Annotated[set[str], _set_union] = Field(default_factory=set)
