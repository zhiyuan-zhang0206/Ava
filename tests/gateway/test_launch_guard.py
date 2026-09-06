"""The autouse agent-launch guard.

Every test runs with `ops.agent_launch._launch_agent_process` replaced by a
recording no-op spy, so a test that triggers a spawn but forgets to stub the
launch never forks a real agent process. Tests that want to assert a
launch happened take the `launched_agents` fixture (the same recorder list).

This is the safety net that ~86 byte-identical
`monkeypatch.setattr("ops.agent_launch._launch_agent_process", lambda _id, **_kw:
None)` stubs used to provide one test at a time.
"""

import psycopg
import pytest


def test_autouse_guard_replaces_real_launch() -> None:
    """Without any explicit stub or fixture request, the real process-forking
    `_launch_agent_process` is already shadowed by the guard spy."""
    import ops.agent_launch

    assert getattr(ops.agent_launch._launch_agent_process, "_ava_test_guard", False)


def test_launched_agents_records_spawn(db_conn: psycopg.Connection, launched_agents: list) -> None:
    """The `launched_agents` recorder captures the id (and config overlay) of
    every launch, replacing per-test `_record` stubs."""
    from ops.agent_spawn import create_agent_row
    from shared.machine import machine_name

    new_id, _birth_config = create_agent_row(
        machine=machine_name(), config={"llm_model": "gpt-5.6-sol"}
    )
    # The runner-side launch (stubbed by the guard spy — the same shape
    # launch_agent_op uses).
    import ops.agent_launch

    ops.agent_launch._launch_agent_process(
        new_id, config_overlay={"llm_model": "gpt-5.6-sol"}, confirm=False
    )

    assert [c.agent_id for c in launched_agents] == [new_id]  # pyright: ignore[reportUnknownMemberType]
    assert launched_agents[0].config_overlay == {"llm_model": "gpt-5.6-sol"}  # pyright: ignore[reportUnknownMemberType]


def test_spawn_preflight_raises_without_agent_runner_capability(
    db_conn: psycopg.Connection,
    set_machine_identity,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The gateway's spawn preflight is the defense behind the router 400: a
    target without the agent-runner capability cannot run agents (the launch op
    re-checks the capability on the runner itself as defense-in-depth)."""
    import pytest

    from gateway.routers.agents import _spawn_preflight_blocking
    from ops.rpc_schemas import SpawnAgentRequest
    from shared.agents import SpawnTargetNotAgentRunner

    set_machine_identity(role="gateway", name="gw-only")
    monkeypatch.setattr("shared.machines.lookup_role", lambda _name: ["gateway"])  # pyright: ignore[reportUnknownArgumentType]
    with pytest.raises(SpawnTargetNotAgentRunner, match="agent-runner"):
        _spawn_preflight_blocking("gw-only", SpawnAgentRequest(spawner="user"), object())  # type: ignore[arg-type]
