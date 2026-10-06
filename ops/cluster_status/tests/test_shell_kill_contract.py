"""One home-runner vocabulary; transport absence cannot enter the RPC contract."""

import pytest
from pydantic import ValidationError

from base.agents import ShellKillMode
from ops import cluster, cluster_status
from ops.rpc_schemas import ShellKillResult


@pytest.mark.parametrize("mode", list(ShellKillMode))
def test_rpc_and_operation_use_the_same_owner(
    monkeypatch: pytest.MonkeyPatch,
    mode: ShellKillMode,
) -> None:
    assert ShellKillResult.model_fields["mode"].annotation is ShellKillMode
    wire = {"mode": mode.value, "interrupted": mode is ShellKillMode.KILLED, "name": None}
    parsed = ShellKillResult.model_validate(wire)
    assert parsed.mode is mode
    assert parsed.model_dump(mode="json") == wire

    def kill(_agent: int, _session: int) -> tuple[ShellKillMode, bool, str | None]:
        return mode, mode is ShellKillMode.KILLED, None

    monkeypatch.setattr(cluster, "kill_shell", kill)
    assert cluster.shell_kill_op(7, 3).mode is mode
    schema = ShellKillResult.model_json_schema()
    ref = schema["properties"]["mode"]["$ref"]
    assert schema["$defs"][ref.rsplit("/", 1)[1]]["enum"] == ["killed", "absent"]


@pytest.mark.parametrize("raw", ["machine_absent", "later", None])
def test_rpc_rejects_transport_and_unknown_modes(raw: object) -> None:
    with pytest.raises(ValidationError):
        ShellKillResult.model_validate({"mode": raw})


def test_host_producer_returns_the_canonical_absent_member(monkeypatch: pytest.MonkeyPatch) -> None:
    def no_shells(_agent: int) -> list[object]:
        return []

    monkeypatch.setattr(cluster_status, "agent_shell_sessions", no_shells)
    mode, interrupted, name = cluster_status.kill_shell(7, 3)
    assert mode is ShellKillMode.ABSENT
    assert interrupted is False
    assert name is None


def test_operation_rejects_unknown_host_verdict(monkeypatch: pytest.MonkeyPatch) -> None:
    def invalid_kill(_agent: int, _session: int) -> tuple[str, bool, str | None]:
        return "machine_absent", False, None

    monkeypatch.setattr(cluster, "kill_shell", invalid_kill)
    with pytest.raises(ValueError, match="ShellKillMode"):
        cluster.shell_kill_op(7, 3)
