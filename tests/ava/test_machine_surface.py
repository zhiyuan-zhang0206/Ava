"""The machine concepts are an unconditional part of the agent-facing SDK.

`ava.agents.spawn` always carries a `machine` parameter, `list_machines` /
`Machine` are always in `ava.agents.__all_for_ava__`, and `ava.self` always exports
`MACHINE_SPEC` + `SELF_MACHINE_NAME`. The surface is no longer gated on any
deployment flag — a single box is just the special case where every spawn
targets the one registered machine.

These assert in-process (the surface is not chosen at import time, so there is
nothing to vary across subprocess envs). `MACHINE_SPEC` / `SELF_MACHINE_NAME`
resolve machine_name() lazily from the session $AVA_HOME (tests/conftest.py
writes it), so the access succeeds.
"""

from __future__ import annotations

import contextlib
import inspect
import io
from datetime import datetime

import ava
import ava.agents
import ava.self
from ava.agents import AgentRow
from shared.agents import AgentStatus


def test_spawn_has_machine_arg() -> None:
    """spawn carries a `machine` parameter and its docstring names the concept."""
    sig = inspect.signature(ava.agents.spawn)
    assert "machine" in sig.parameters, sig
    assert "machine" in (inspect.getdoc(ava.agents.spawn) or "").lower()


def test_machine_names_in_agents_all() -> None:
    """list_machines + Machine render into the ava.agents SDK docs."""
    assert "list_machines" in ava.agents.__all_for_ava__
    assert "Machine" in ava.agents.__all_for_ava__
    assert callable(ava.agents.list_machines)


def test_self_exports_machine_consts() -> None:
    """ava.self exports both machine identity constants."""
    assert "MACHINE_SPEC" in ava.self.__all_for_ava__
    assert "SELF_MACHINE_NAME" in ava.self.__all_for_ava__
    # Both resolve lazily via module __getattr__ against the session $AVA_HOME.
    assert ava.self.MACHINE_SPEC is not None
    assert isinstance(str(ava.self.SELF_MACHINE_NAME), str)


def test_agents_help_mentions_machines() -> None:
    """The rendered ava.agents doc — what the agent reads — names the machine
    surface (spawn's `machine` arg, list_machines)."""
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        ava.help(ava.agents)
    text = buf.getvalue().lower()
    assert "machine" in text, text


def test_agent_row_str_includes_machine() -> None:
    """machine is a real column on the dataclass and always renders in str()."""
    assert "machine" in AgentRow.__annotations__
    now = datetime.now().astimezone()
    row = AgentRow(
        agent_id=1,
        label=None,
        status=AgentStatus.RUNNING,
        spawner="user",
        machine="m1",
        spawned_at=now,
        started_at=now,
        last_active_at=now,
        pid=42,
        heartbeat_paused_until=None,
    )
    assert row.machine == "m1"
    assert "machine=m1" in str(row)
