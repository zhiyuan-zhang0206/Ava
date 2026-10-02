"""The gateway's cluster status gather: the local probe, identity mismatch episodes, row order, the paused reason and the retired projections."""

import asyncio
import logging
from datetime import UTC, datetime

import pytest

import gateway.cluster.status as status_mod
from gateway.cluster import roster_probe
from ops import cluster_status


def test_gather_cluster_status_local_agent_runner_probed(monkeypatch: pytest.MonkeyPatch):
    """A local agent-runner row is probed via its own ops server (status_probe,
    dialed at its registered localhost URL), picking up shell_count + daemon
    health from the op result — same path as any remote machine."""

    async def _fake_dispatch(
        *,
        target_machine,
        kind,
        payload,
        timeout_s=None,
        ops_url=None,
        retries=None,
        idempotency_key=None,
    ):
        assert kind == "status_probe"
        assert target_machine == "m1"
        assert ops_url == "http://localhost:9"
        # The real ops server always echoes its own machine_name in the ClusterStatus
        # probe result; the gateway verifies it matches the targeted row.
        return {
            "machine_name": "m1",
            "serve_gateway": True,
            "serve_agent_runner": True,
            "paused": False,
            "head_sha": "abc123",
            "running_sha": "def456",
            "shell_count": 4,
            "agent_host_online": True,
            "supervisor_online": False,
        }

    monkeypatch.setattr(status_mod._cluster_rpc, "dispatch_to_machine", _fake_dispatch)  # pyright: ignore[reportUnknownArgumentType]

    rows: list[tuple[str, str | None, list[str], datetime, str | None, datetime | None, bool]] = [
        (
            "m1",
            "http://localhost:9",
            ["agent-runner", "gateway"],
            datetime.now(UTC),
            None,
            None,
            False,
        )
    ]
    machines = asyncio.run(status_mod.gather_cluster_status(rows, "m1"))

    assert len(machines) == 1
    m = machines[0]
    assert m.online is True
    assert m.identity_mismatch is False
    assert m.head_sha == "abc123"
    assert m.running_sha == "def456"
    assert m.shell_count == 4
    assert m.agent_host_online is True
    assert m.supervisor_online is False


def test_probe_flags_identity_mismatch_when_responder_name_differs(monkeypatch: pytest.MonkeyPatch):
    """If the ops server answers under a machine_name != the targeted row, the
    probe returns a loud identity-mismatch row (online False, identity_mismatch
    True) instead of a false-green online. Guards the 2026-07-18 incident where a
    loopback gateway_url made the gateway dial itself and answer under its own name."""

    async def _fake_dispatch(
        *,
        target_machine,
        kind,
        payload,
        timeout_s=None,
        ops_url=None,
        retries=None,
        idempotency_key=None,
    ):
        assert target_machine == "air"
        assert ops_url == "http://localhost:8106"
        # The gateway (co-located ops server) answers under ITS name, not "air" — a
        # complete ClusterStatus (so it validates), just from the wrong host.
        return {
            "machine_name": "gateway-host",
            "serve_gateway": True,
            "serve_agent_runner": True,
            "paused": False,
            "head_sha": "abc123",
        }

    monkeypatch.setattr(status_mod._cluster_rpc, "dispatch_to_machine", _fake_dispatch)  # pyright: ignore[reportUnknownArgumentType]

    rows: list[tuple[str, str | None, list[str], datetime, str | None, datetime | None, bool]] = [
        ("air", "http://localhost:8106", ["agent-runner"], datetime.now(UTC), None, None, False)
    ]
    machines = asyncio.run(status_mod.gather_cluster_status(rows, "gateway-host"))

    assert len(machines) == 1
    m = machines[0]
    assert m.name == "air"
    assert m.identity_mismatch is True
    assert m.online is False
    # It did NOT pick up the impostor's data.
    assert m.head_sha is None


def test_identity_mismatch_logs_once_per_episode(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
):
    """An active row's mismatch stays a loud ERROR, but one line per episode —
    not one per panel poll; a correct identity echo ends the episode, so a
    later mismatch logs anew (task #4143)."""
    monkeypatch.setattr(roster_probe, "_identity_mismatch_active", set[str]())
    responder = {"name": "gateway-host"}

    async def _fake_dispatch(
        *,
        target_machine,
        kind,
        payload,
        timeout_s=None,
        ops_url=None,
        retries=None,
        idempotency_key=None,
    ):
        return {
            "machine_name": responder["name"],
            "serve_gateway": True,
            "serve_agent_runner": True,
            "paused": False,
            "head_sha": "abc123",
        }

    monkeypatch.setattr(status_mod._cluster_rpc, "dispatch_to_machine", _fake_dispatch)  # pyright: ignore[reportUnknownArgumentType]
    rows: list[tuple[str, str | None, list[str], datetime, str | None, datetime | None, bool]] = [
        ("air", "http://localhost:8106", ["agent-runner"], datetime.now(UTC), None, None, False)
    ]
    caplog.set_level(logging.DEBUG, logger="gateway.cluster.roster_probe")

    machines = asyncio.run(status_mod.gather_cluster_status(rows, "gateway-host"))
    assert machines[0].identity_mismatch is True
    first = [r for r in caplog.records if r.name == "gateway.cluster.roster_probe"]
    assert [r.levelno for r in first] == [logging.ERROR]

    # A second poll of the same mismatch is silent.
    caplog.clear()
    asyncio.run(status_mod.gather_cluster_status(rows, "gateway-host"))
    assert [r for r in caplog.records if r.name == "gateway.cluster.roster_probe"] == []

    # The identity echoes correctly -> the episode ends...
    caplog.clear()
    responder["name"] = "air"
    machines = asyncio.run(status_mod.gather_cluster_status(rows, "gateway-host"))
    assert machines[0].online is True

    # ...so the next mismatch is a fresh episode and logs again.
    responder["name"] = "gateway-host"
    asyncio.run(status_mod.gather_cluster_status(rows, "gateway-host"))
    again = [
        r
        for r in caplog.records
        if r.name == "gateway.cluster.roster_probe" and r.levelno == logging.ERROR
    ]
    assert len(again) == 1


def test_identity_mismatch_on_stopped_machine_is_info_once(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
):
    """A stopped row's stale URL answering as another host is the expected
    face of the stop: the verdict stays (the row is still not that host), but
    the line degrades to INFO and is reported once per episode (task #4143)."""
    monkeypatch.setattr(roster_probe, "_identity_mismatch_active", set[str]())

    async def _fake_dispatch(
        *,
        target_machine,
        kind,
        payload,
        timeout_s=None,
        ops_url=None,
        retries=None,
        idempotency_key=None,
    ):
        return {
            "machine_name": "gateway-host",
            "serve_gateway": True,
            "serve_agent_runner": True,
            "paused": False,
            "head_sha": "abc123",
        }

    monkeypatch.setattr(status_mod._cluster_rpc, "dispatch_to_machine", _fake_dispatch)  # pyright: ignore[reportUnknownArgumentType]
    stopped = datetime.now(UTC)
    rows: list[tuple[str, str | None, list[str], datetime, str | None, datetime | None, bool]] = [
        ("air", "http://localhost:8106", ["agent-runner"], stopped, None, stopped, False)
    ]
    caplog.set_level(logging.DEBUG, logger="gateway.cluster.roster_probe")

    machines = asyncio.run(status_mod.gather_cluster_status(rows, "gateway-host"))
    asyncio.run(status_mod.gather_cluster_status(rows, "gateway-host"))

    assert machines[0].identity_mismatch is True
    records = [r for r in caplog.records if r.name == "gateway.cluster.roster_probe"]
    assert [r.levelno for r in records] == [logging.INFO]
    assert "stopped machine" in records[0].getMessage()


def test_gather_cluster_status_local_pure_gateway_lightweight(monkeypatch: pytest.MonkeyPatch):
    """A local machine WITHOUT agent-runner capability (pure gateway) runs no
    ops server: its row is a lightweight local read — no probe dispatched, no
    session/pidfile reads, agent-runner-only fields at their defaults."""
    dispatched: list[str] = []

    async def _fake_dispatch(**kwargs):
        dispatched.append(kwargs["kind"])  # pyright: ignore[reportUnknownArgumentType]
        return {}

    monkeypatch.setattr(status_mod._cluster_rpc, "dispatch_to_machine", _fake_dispatch)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(status_mod, "cluster_is_paused", lambda: True)
    monkeypatch.setattr(status_mod, "prod_source_head_sha", lambda: "abc123")

    rows: list[tuple[str, str | None, list[str], datetime, str | None, datetime | None, bool]] = [
        ("m1", "http://m1", ["gateway"], datetime.now(UTC), None, None, False)
    ]
    machines = asyncio.run(status_mod.gather_cluster_status(rows, "m1"))

    assert dispatched == []
    m = machines[0]
    assert m.online is True
    assert m.paused is True
    assert m.head_sha == "abc123"
    assert m.shell_count == 0
    assert m.agent_host_online is None
    assert m.supervisor_online is None


def test_gather_returns_rows_sorted_by_name(monkeypatch: pytest.MonkeyPatch):
    """The local pure-gateway row takes the lightweight local read and the
    address-less row is reported offline without a dial; the roster comes back
    ordered by machine name whatever order the table gave."""
    monkeypatch.setattr(status_mod, "cluster_is_paused", lambda: False)
    monkeypatch.setattr(status_mod, "prod_source_head_sha", lambda: "abc123")
    now = datetime.now(UTC)
    rows: list[tuple[str, str | None, list[str], datetime, str | None, datetime | None, bool]] = [
        ("m2", None, ["agent-runner"], now, None, None, False),
        ("m1", "http://m1", ["gateway"], now, None, None, False),
    ]

    machines = asyncio.run(status_mod.gather_cluster_status(rows, "m1"))

    assert [m.name for m in machines] == ["m1", "m2"]


def test_gather_cluster_status_carries_probe_paused_reason(monkeypatch: pytest.MonkeyPatch):
    """A host parked by its serving gate must arrive on the roster with the
    breakdown attached (task #3404): paused=true + paused_reason='startup', not
    one opaque bool a consumer can misread as a deliberate pause."""

    async def _fake_dispatch(
        *,
        target_machine,
        kind,
        payload,
        timeout_s=None,
        ops_url=None,
        retries=None,
        idempotency_key=None,
    ):
        assert kind == "status_probe"
        assert target_machine == "m1"
        return {
            "machine_name": "m1",
            "serve_gateway": True,
            "serve_agent_runner": True,
            "paused": True,
            "paused_reason": "startup",
            "head_sha": "abc123",
        }

    monkeypatch.setattr(status_mod._cluster_rpc, "dispatch_to_machine", _fake_dispatch)  # pyright: ignore[reportUnknownArgumentType]

    rows: list[tuple[str, str | None, list[str], datetime, str | None, datetime | None, bool]] = [
        ("m1", "http://localhost:9", ["agent-runner"], datetime.now(UTC), None, None, False)
    ]
    machines = asyncio.run(status_mod.gather_cluster_status(rows, "m1"))

    assert len(machines) == 1
    m = machines[0]
    assert m.online is True
    assert m.paused is True
    assert m.paused_reason == "startup"


def test_status_schemas_omit_retired_updater_projections() -> None:
    from base.api_contracts.status import MachineStatus
    from gateway.cluster.schemas import ClusterPanel

    retired = {"current_orchestration", "last_updater_outcome", "last_update"}
    for model in (cluster_status.ClusterStatus, ClusterPanel, MachineStatus):
        assert retired.isdisjoint(model.model_fields)
