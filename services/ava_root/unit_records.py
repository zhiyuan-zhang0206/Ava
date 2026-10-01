"""The per-unit records the supervisor and its stop family share.

`_Generation` is one process instance of a unit; `_UnitRuntime` is its mutable
supervisor-side record. They live beside — not inside — `supervisor.py` so the
stop family can be a separate module within the file budget without a circular
import.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

from base.native_process.ownership import OwnedProcess
from services.ava_root.custody import ServiceCustody
from services.ava_root.intent_store import IntentSource, RestartFailure, UnitIntent
from services.ava_root.manifest import UnitManifest, UnitState


@dataclass(slots=True)
class _Generation:
    """One process instance of a unit.

    `exited` is set as the *last* action of the wait task, so any coroutine
    that observes the event also observes the exit fully processed.
    """

    proc: asyncio.subprocess.Process
    started_at: float
    identity: OwnedProcess | None = None
    """The leader's native birth; None when the leader exited before root read it.

    Root, its only reaper, reaped it or will; its group is still `proc.pid`."""
    custody: ServiceCustody | None = None
    tracked: set[OwnedProcess] = field(default_factory=set[OwnedProcess])
    closing: bool = False
    scope_closed_at_exit: bool = False
    """The leader's group was empty when the watch read it after the reap.

    Nothing of the unit remained. Otherwise the watch retained the group's
    members in `tracked` at that read."""
    exited: asyncio.Event = field(default_factory=asyncio.Event)


@dataclass(slots=True)
class _UnitRuntime:
    """Mutable per-unit state owned by the supervisor.

    `intent` is the policy fact — what an operator or the deployment asked for
    — and the only run/stop source classification reads; `restart_failed` is
    the explicit failure state of an interrupted replacement. Both survive a
    root restart in this unit's intent record.
    """

    manifest: UnitManifest
    intent: UnitIntent = UnitIntent.STOPPED
    intent_source: IntentSource = IntentSource.SELECTION
    restart_failed: RestartFailure | None = None
    state: UnitState = UnitState.STOPPED
    generation: _Generation | None = None
    restart_count: int = 0
    last_exit: str | None = None
    last_error: str | None = None
    watch_task: asyncio.Task[None] | None = None
