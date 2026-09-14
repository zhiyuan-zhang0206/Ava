"""The optional wiring hook: assemble deployment-side participants by name.

The daemon itself stays deployment-neutral — it never imports deployment
code. `--wiring module:attr` is the one explicit seam: when given, the daemon
imports that module, calls `attr(context)` once, and drives the returned
participant(s) with its own lifecycle. Without the flag nothing here runs and
the daemon behaves exactly as before.

Contract (v1, W1.2e):

- `context` is a :class:`WiringContext` — the supervisor, the unit registry,
  and the run-dir facts a deployment assembly may need.
- `attr(context)` returns one participant or a list/tuple of participants.
  A participant exposes `start()` / `stop()`; either may be sync or return an
  awaitable (the runner awaits a coroutine result).
- Everything is fail-fast: a malformed reference, an unimportable module, a
  missing or raising callable, or an invalid return value raises
  :class:`WiringError` before the tree starts — a half-wired daemon is never
  left running. Start-up of a participant that fails after others started
  stops those again (reverse order) and still aborts the daemon.
"""

from __future__ import annotations

import importlib
import inspect
import logging
from collections.abc import Awaitable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, cast, runtime_checkable

from services.ava_root.manifest import UnitRegistry
from services.ava_root.supervisor import Supervisor

_log = logging.getLogger("ava_root")


class WiringError(ValueError):
    """The wiring reference is malformed, cannot resolve, or yields no participant."""


@runtime_checkable
class WiringParticipant(Protocol):
    """One deployment-side assembly the daemon drives alongside its tree."""

    def start(self) -> Awaitable[None] | None:
        """Begin the participant's work; a failure aborts the daemon start."""
        ...

    def stop(self) -> Awaitable[None] | None:
        """Stop the participant's work; called in reverse start order."""
        ...


@dataclass(frozen=True, slots=True)
class WiringContext:
    """Everything the wiring factory may touch — public daemon facts only."""

    supervisor: Supervisor
    registry: UnitRegistry
    run_dir: Path
    log_dir: Path


def _split_ref(spec: str) -> tuple[str, str]:
    module_name, sep, attribute = spec.partition(":")
    if not sep or not module_name or not attribute or ":" in attribute:
        raise WiringError(f"wiring reference {spec!r} is not 'module:attribute'")
    return module_name, attribute


def load_wiring(spec: str | None, context: WiringContext) -> tuple[WiringParticipant, ...]:
    """Resolve `--wiring module:attr` into participants; None yields none.

    All resolution happens here, before any tree state exists: an import
    error, a missing attribute, a non-callable, a raising factory, or an
    invalid return value all raise `WiringError`.
    """
    if spec is None:
        return ()
    module_name, attribute = _split_ref(spec)
    try:
        module = importlib.import_module(module_name)
    except Exception as exc:
        raise WiringError(f"cannot import wiring module {module_name!r}: {exc}") from exc
    try:
        factory = getattr(module, attribute)
    except AttributeError as exc:
        raise WiringError(f"module {module_name!r} has no attribute {attribute!r}") from exc
    if not callable(factory):
        raise WiringError(f"wiring reference {spec!r} resolves to a non-callable")
    try:
        produced = factory(context)
    except Exception as exc:
        raise WiringError(f"wiring factory {spec!r} raised: {exc}") from exc
    if isinstance(produced, (list, tuple)):
        candidates: tuple[object, ...] = tuple(cast("Sequence[object]", produced))
    else:
        candidates = (produced,)
    participants: list[WiringParticipant] = []
    for index, candidate in enumerate(candidates):
        if inspect.isclass(candidate):
            raise WiringError(f"wiring {spec!r} participant #{index} is a class, not an instance")
        if not isinstance(candidate, WiringParticipant):
            raise WiringError(
                f"wiring {spec!r} participant #{index} lacks start/stop: {candidate!r}"
            )
        participants.append(candidate)
    _log.info("wiring %s produced %d participant(s)", spec, len(participants))
    return tuple(participants)


async def start_participants(
    participants: Sequence[WiringParticipant],
) -> list[WiringParticipant]:
    """Start each participant in order; a failure stops the already-started.

    Returns the participants that started (the daemon stops exactly these on
    shutdown, in reverse order).
    """
    started: list[WiringParticipant] = []
    for participant in participants:
        try:
            result = participant.start()
            if inspect.isawaitable(result):
                await result
        except Exception as exc:
            await stop_participants(started)
            raise WiringError(f"wiring participant failed to start: {exc}") from exc
        started.append(participant)
    return started


async def stop_participants(started: Sequence[WiringParticipant]) -> None:
    """Stop participants in reverse start order; a failing stop never blocks the rest."""
    for participant in reversed(started):
        try:
            result = participant.stop()
            if inspect.isawaitable(result):
                await result
        except Exception:
            _log.exception("wiring participant failed to stop")
