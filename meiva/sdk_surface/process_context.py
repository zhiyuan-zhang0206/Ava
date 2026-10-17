"""The process-local SDK entry: its context and exec-turn state slots, and the contexts built for them.

The execution child, schedule runner and external attachment publish their context
here; ``meiva.context`` / ``meiva.state`` / ``meiva.state_update`` on the ``meiva`` module are attribute
delegations to the slots below. Hosted graph runs receive their context through LangGraph Runtime
and never publish an agent identity: a shared host only records its posture (`bind_host_process`).
The slots are kept in the ``meiva`` module object's own dictionary (as the SDK installation is), so
reloading ``meiva`` neither resets nor duplicates them. This module owns no thread patch or live
clients; the clients a context carries are built lazily by `process_clients`.

Framework-internal. Plugin authors do not touch the slots: exec-side state read/write goes through
`agent.state.PluginStateHandle`, and host-side logic (graph hooks) never touches them at all - it
reads the graph `state` argument and returns an update dict.

Lifecycle of the exec-turn state slot (framework side): before agent code runs, the exec child
(after loading plugins) sets `set_state(<snapshot validated from the request envelope>)` and
`set_state_update({})` (`agent/execution/child.py:build_state_slot`). `PluginStateHandle.read`
reads the state; `.update` synchronously mutates its working copy and accumulates the raw delta
into the update dict. When the child exits it writes the update dict into its result envelope;
exec_node reads it from there and merges it into `Command(update=...)`, going through the
LangGraph reducer. Nothing resets the slots in the child: they are discarded with the process. An
external attachment unbinds them at detach. No other process - the agent host included - ever binds
them. Under the LangGraph cycling topology nodes run sequentially, so there is no cross-turn race;
if parallel branches (fan-out) are introduced, this one-slot model must be re-evaluated.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any, cast

from base.agents.context import MeivaContext
from base.native_process.loaded_commit import LoadedCommit

from . import meiva_module


class SdkProcessPurpose(StrEnum):
    """Non-agent startup posture stored in the SDK's existing process entry."""

    SHARED_HOST = "shared_host"


class ContextOutsideProcessError(AttributeError):
    """The SDK's local context was read outside an execution process."""


class PluginStateOutsideTurnError(AttributeError):
    """`meiva.state` / `meiva.state_update` (or a `PluginStateHandle` read / update) touched
    outside an exec turn - the framework binds the state slot only in the exec child that
    runs agent code.

    An AttributeError, so the attribute simply does not exist elsewhere. Common misuse: a
    plugin reads state at module load time, or from a graph hook in the agent host - host
    logic reads the graph `state` argument its hook was handed, never the SDK's slot.
    """


# The three slots live on the `meiva` module object itself, like the installation
# (`installation.SLOT`): the SDK entry is the one process-wide thing they describe, and its
# dictionary survives `importlib.reload(meiva)`. `_context` holds a bound `MeivaContext`, the shared
# host's posture marker or nothing; `_state` / `_state_update` exist only while an exec turn is
# bound. Reading the state slots anywhere else raises `PluginStateOutsideTurnError`, never a None.
_CONTEXT_KEY = "_exec_context"
_STATE_KEY = "_exec_state"
_UPDATE_KEY = "_exec_state_update"
_IMAGE_KEY = "_LOADED_IMAGE"


def _slots() -> dict[str, Any]:
    return meiva_module().__dict__


def _bound() -> MeivaContext | SdkProcessPurpose | None:
    return _slots().get(_CONTEXT_KEY)


def outside_exec_turn(name: str) -> PluginStateOutsideTurnError:
    return PluginStateOutsideTurnError(
        f"meiva.{name} exists only inside execute_code (an exec turn); this process has no bound "
        "turn state. Host code reads the graph state its hook was handed instead."
    )


def outside_process(name: str) -> ContextOutsideProcessError:
    return ContextOutsideProcessError(
        f"meiva.{name} requires an execution child, launched script or external attachment; "
        "host code uses the context its caller supplied"
    )


def get_state() -> Any:
    """The exec turn's working state, or `PluginStateOutsideTurnError` outside a turn."""
    value = _slots().get(_STATE_KEY)
    if value is None:
        raise outside_exec_turn("state")
    return value


def set_state(value: Any) -> None:
    """Bind the exec turn's working state; end the turn with `unbind_exec_turn`."""
    if value is None:
        raise TypeError(
            "meiva.state cannot be None; end a turn with process_context.unbind_exec_turn()"
        )
    _slots()[_STATE_KEY] = value


def get_state_update() -> Any:
    """Whatever the exec code left in the delta slot (the exec child validates it at exit)."""
    slots = _slots()
    if slots.get(_STATE_KEY) is None:
        raise outside_exec_turn("state_update")
    return slots.get(_UPDATE_KEY)


def set_state_update(value: Any) -> None:
    _slots()[_UPDATE_KEY] = value


def in_exec_turn() -> bool:
    """Whether this process is running an exec turn - the framework has bound its state slot.

    The one explicit answer for the SDK's own call sites (cwd-aware wraps, the security
    scan): true in an exec child while agent code runs and in an attached external
    controller, false in the agent host and in bare scripts. Framework-internal - not in the
    `meiva.help()` view.
    """
    return _slots().get(_STATE_KEY) is not None


def unbind_exec_turn() -> None:
    """End the bound turn: drop both slot values (an external attachment's detach, a test)."""
    slots = _slots()
    slots.pop(_STATE_KEY, None)
    slots.pop(_UPDATE_KEY, None)


def is_host_process() -> bool:
    """Whether this interpreter explicitly booted as the shared host."""
    return _bound() is SdkProcessPurpose.SHARED_HOST


def bind_host_process() -> None:
    """Establish shared-host startup posture before plugins or graph work.

    This process never binds a current agent to the SDK, even if its environment
    inherited an agent id. The posture lasts until this interpreter exits.
    """
    bound = _bound()
    if bound is not None and bound is not SdkProcessPurpose.SHARED_HOST:
        raise RuntimeError("cannot boot a shared host with an established SDK process context")
    _slots()[_CONTEXT_KEY] = SdkProcessPurpose.SHARED_HOST


def bound_context() -> MeivaContext | None:
    """Inspect the existing entry without turning host posture into a context."""
    bound = _bound()
    return None if isinstance(bound, SdkProcessPurpose) else bound


def require_bound_context() -> MeivaContext:
    """The bound context, or `ContextOutsideProcessError` where this process has none.

    The raw slot read behind the `meiva.context` attribute; it never builds a launched context."""
    bound = _bound()
    if bound is SdkProcessPurpose.SHARED_HOST:
        raise ContextOutsideProcessError(
            "the shared agent host has no SDK process context; "
            "host code uses the context its caller supplied"
        )
    if bound is None:
        raise outside_process("context")
    return bound


def current_context() -> MeivaContext:
    """What `meiva.context` answers: the bound context, a launched script's own on its first read,
    or `ContextOutsideProcessError`. Read through the live `meiva` module, which owns that rule."""
    return meiva_module().context


def ambient_context() -> MeivaContext | None:
    """`meiva.context` where this process has one, else None."""
    try:
        return current_context()
    except ContextOutsideProcessError:
        return None


def bind_context(context: MeivaContext) -> None:
    """Initialize this process's SDK entry with an explicit context.

    Framework-internal: used by disposable children and exclusive attachments,
    never by the shared host to select its current agent.
    """
    if is_host_process():
        raise RuntimeError("the shared agent host cannot bind an SDK process context")
    if not isinstance(context, MeivaContext):
        raise TypeError("meiva.context must be an MeivaContext")
    _slots()[_CONTEXT_KEY] = context


def unbind_context() -> MeivaContext | None:
    """Release the SDK binding without creating a launched-script context.

    The caller owns restoration and closing its clients.
    """
    if is_host_process():
        raise RuntimeError("the shared agent host cannot release its startup posture")
    return cast(MeivaContext | None, _slots().pop(_CONTEXT_KEY, None))


def loaded_code_image() -> LoadedCommit:
    """The immutable image captured at this SDK module's first load.

    Process composition roots consume this fact without rereading Git or relabeling
    already-loaded Python after a checkout move or an `meiva` reload. The value is recorded on the
    `meiva` module object by `meiva/__init__.py` the first time that module executes.
    """
    return _slots()[_IMAGE_KEY]
