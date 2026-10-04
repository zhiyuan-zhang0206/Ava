"""The exec turn's state slot: `ava.state` / `ava.state_update` exist only inside an exec turn.

Outside one — the agent host, a bare script — reading either raises an explicit AttributeError
instead of returning None, `ava.in_exec_turn()` is the one explicit predicate, and a plugin's
`PluginStateHandle` has a pure host side (`view` / `delta`) beside the exec-side `read` / `update`.
"""

from pathlib import Path
from typing import Annotated, Any

import pytest
from pydantic import BaseModel, Field

import ava
from agent.state import (
    BaseAgentState,
    CompactState,
    PluginStateHandle,
    build_agent_state,
    compact_version,
)
from base.packages.plugins.extensions import ExtensionRegistry, PluginContributions


@pytest.fixture(autouse=True)
def _unbound():
    ava.unbind_exec_turn()
    yield
    ava.unbind_exec_turn()


def _set_union(current: set[str], new: set[str]) -> set[str]:
    return current | new


def test_in_exec_turn_follows_the_bound_slot():
    assert ava.in_exec_turn() is False
    ava.state = BaseAgentState()
    assert ava.in_exec_turn() is True
    ava.unbind_exec_turn()
    assert ava.in_exec_turn() is False


def test_state_slots_do_not_exist_outside_an_exec_turn():
    """Reading either slot anywhere but a bound exec turn raises an explicit AttributeError —
    never a None — so a host-side read fails where it is written."""
    for name in ("state", "state_update"):
        with pytest.raises(ava.PluginStateOutsideTurnError, match="only inside execute_code"):
            getattr(ava, name)
    assert isinstance(ava.PluginStateOutsideTurnError("x"), AttributeError)
    assert not hasattr(ava, "state")
    assert not hasattr(ava, "state_update")


def test_state_slots_are_readable_once_bound_and_gone_after_unbind():
    ava.state = BaseAgentState()
    ava.state_update = {}
    assert ava.state_update == {}
    ava.unbind_exec_turn()
    with pytest.raises(ava.PluginStateOutsideTurnError):
        _ = ava.state
    with pytest.raises(ava.PluginStateOutsideTurnError):
        _ = ava.state_update


def test_state_cannot_be_assigned_none():
    with pytest.raises(TypeError, match="unbind_exec_turn"):
        ava.state = None


def test_compact_version_reads_the_bound_snapshot_and_refuses_outside_a_turn():
    with pytest.raises(ava.PluginStateOutsideTurnError):
        compact_version()
    ava.state = BaseAgentState(compact=CompactState(version=3))
    assert compact_version() == 3


class _SeenState(BaseModel):
    seen: Annotated[set[str], _set_union] = Field(default_factory=set)
    counter: int = 0


def test_handle_read_and_update_outside_a_turn_raise_the_slot_error():
    handle = PluginStateHandle(_SeenState, "demo")
    with pytest.raises(ava.PluginStateOutsideTurnError):
        handle.read()
    with pytest.raises(ava.PluginStateOutsideTurnError):
        handle.update({"counter": 1})


def test_handle_view_and_delta_are_pure_over_the_graph_state():
    """The host-side path: `view` builds the typed snapshot from a graph state, `delta` the
    prefixed update dict a hook returns — neither needs (or has) an exec slot."""
    state_cls = build_agent_state(
        ExtensionRegistry((("demo", PluginContributions(state=(_SeenState,))),))
    )
    fields: dict[str, Any] = {"demo__counter": 5, "demo__seen": {"a"}}
    graph_state = state_cls(messages=[], halted=False, **fields)
    handle = PluginStateHandle(_SeenState, "demo")

    assert not ava.in_exec_turn()
    view = handle.view(graph_state)
    assert (view.counter, view.seen) == (5, {"a"})
    assert handle.delta({"counter": 6, "seen": {"b"}}) == {"demo__counter": 6, "demo__seen": {"b"}}
    with pytest.raises(ValueError, match="unknown field"):
        handle.delta({"typo": 1})


def test_child_clients_are_lazy_and_released_when_the_process_ends(tmp_path: Path) -> None:
    """A connection the child's code never touches is never built; one it touches is closed by the
    child itself before it exits."""
    from agent.graph.exec.protocol import read_result
    from agent.tests.test_exec_child import _spawn

    marker = tmp_path / "closed"
    code = f"""
import ava
from base.agents.context.clients import ClientSet
from pathlib import Path

class Conn:
    closed = False
    broken = False
    def close(self):
        Path({str(marker)!r}).write_text("closed")

ClientSet._connect_sql = lambda self: Conn()
print("before:", repr(ava.context.sql))
ava.DB.closed
print("after:", ava.DB.__class__.__name__)
print("marker before exit:", Path({str(marker)!r}).exists())
"""
    proc, _request, result = _spawn(tmp_path, code)

    assert proc.returncode == 0, proc.stderr
    assert read_result(result).kind == "done"
    assert "before: <lazy sql (not connected)>" in proc.stdout
    assert "marker before exit: False" in proc.stdout
    assert marker.read_text() == "closed"
