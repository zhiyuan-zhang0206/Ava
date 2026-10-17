from __future__ import annotations

import contextlib
import inspect
import io
from datetime import datetime

import pytest

import meiva
import meiva.agents
import meiva.self
from base.api_contracts.agent_contract import AgentStatus
from meiva.agents import AgentRow
from meiva.sdk_surface import process_context


def test_spawn_has_machine_arg() -> None:
    sig = inspect.signature(meiva.agents.spawn)
    assert "machine" in sig.parameters, sig
    assert "machine" in (inspect.getdoc(meiva.agents.spawn) or "").lower()


def test_machine_names_in_agents_all() -> None:
    assert "list_machines" in meiva.agents.__all_for_meiva__
    assert "Machine" in meiva.agents.__all_for_meiva__
    assert callable(meiva.agents.list_machines)


def test_self_exports_machine_consts() -> None:
    assert "MACHINE_SPEC" in meiva.self.__all_for_meiva__
    from base.agents.context import MeivaContext
    from base.cluster.machine import MachineIdentity

    machine = MachineIdentity(
        name=lambda: "test",
        role=lambda: frozenset({"gateway"}),
        host=lambda: "localhost",
        description=lambda: None,
    )
    prior = getattr(meiva, "context", None)
    process_context.bind_context(MeivaContext(machine=machine))
    try:
        assert meiva.self.MACHINE_SPEC == ("test", "")
    finally:
        process_context.unbind_context()
        if prior is not None:
            process_context.bind_context(prior)


@pytest.mark.usefixtures("sdk_model_owner")
def test_agents_help_mentions_machines() -> None:
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        meiva.help(meiva.agents)
    text = buf.getvalue().lower()
    assert "machine" in text, text


def test_agent_row_str_includes_machine() -> None:
    assert "machine" in AgentRow.__annotations__
    now = datetime.now().astimezone()
    row = AgentRow(
        agent_id=1,
        label=None,
        status=AgentStatus.RUNNING,
        spawner="user",
        fork_source_agent_id=None,
        machine="m1",
        spawned_at=now,
        started_at=now,
        last_active_at=now,
        last_inbound_at=now,
        pid=42,
        heartbeat_paused_until=None,
    )
    assert row.machine == "m1"
    assert "machine=m1" in str(row)
