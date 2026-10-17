from __future__ import annotations

from pathlib import Path

import psycopg
import pytest

from base.api_contracts.agent_contract import MachineNotRegistered
from base.cluster import machines
from base.cluster.tests.machines.setup import (
    MachineSetup,
    machines_test_db,
    read_description,
    read_machine,
)
from base.cluster.tests.machines.setup import (
    machine_setup as machine_setup,
)
from base.cluster.tests.machines.setup import (
    truncate_machines as truncate_machines,
)
from base.config import settings
from base.db.code_version_gate import ProcessDbGate


def test_gateway_url_comes_from_settings(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("MEIVA_HOME", str(tmp_path))
    (tmp_path / "gateway_url").write_text("http://from-file:8000")
    monkeypatch.setattr(settings.gateway, "gateway_url", "http://from-env:8000/")
    assert machines.gateway_url(settings.gateway.gateway_url) == "http://from-env:8000"
    monkeypatch.setattr(settings.gateway, "gateway_url", "")
    with pytest.raises(machines.GatewayUrlMissing):
        machines.gateway_url(settings.gateway.gateway_url)


def test_gateway_url_raises_when_blank(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings.gateway, "gateway_url", "   ")
    with pytest.raises(machines.GatewayUrlMissing):
        machines.gateway_url(settings.gateway.gateway_url)


def test_register_self_and_lookup_roundtrip(
    machine_setup: MachineSetup, database_gate: ProcessDbGate
) -> None:
    machine_setup(name="test-rt-machine")
    machine_setup.register(machines_test_db(database_gate=database_gate), url="http://rt:8000")
    assert (
        machines.lookup(machines_test_db(database_gate=database_gate), "test-rt-machine")
        == "http://rt:8000"
    )


def test_register_self_composes_single_unit_row(
    machine_setup: MachineSetup, database_gate: ProcessDbGate
) -> None:
    machine_setup(name="runner-a", role="agent-runner")
    machine_setup.register(
        machines_test_db(database_gate=database_gate), url="http://runner-a:9100"
    )
    gateway_url, role, _desc, stopped_at = read_machine("runner-a")
    assert gateway_url == "http://runner-a:9100"
    assert role == ["agent-runner"]
    assert stopped_at is None


def test_register_self_overwrites_url(
    machine_setup: MachineSetup, database_gate: ProcessDbGate
) -> None:
    machine_setup(name="test-overwrite")
    machine_setup.register(machines_test_db(database_gate=database_gate), url="http://old:8000")
    machine_setup.register(machines_test_db(database_gate=database_gate), url="http://new:8000")
    assert (
        machines.lookup(machines_test_db(database_gate=database_gate), "test-overwrite")
        == "http://new:8000"
    )


def test_register_self_agent_runner_stores_null(
    machine_setup: MachineSetup, database_gate: ProcessDbGate
) -> None:
    machine_setup(name="test-agent-runner", role="agent-runner")
    machine_setup.register(machines_test_db(database_gate=database_gate), url=None)
    with pytest.raises(machines.MachineGatewayUrlMissing):
        machines.lookup(machines_test_db(database_gate=database_gate), "test-agent-runner")


def test_register_self_station_only_composes_row(
    machine_setup: MachineSetup, monkeypatch: pytest.MonkeyPatch, database_gate: ProcessDbGate
) -> None:
    machine_setup.machine.set(host="10.0.0.9")
    machine_setup(name="station-a", role="observability-station")
    machine_setup.register(
        machines_test_db(database_gate=database_gate),
        url=machines.unit_dial_url(
            frozenset({"observability-station"}),
            host=machine_setup.machine.host(),
            ops_port=8600,
            gateway_port=settings.gateway.gateway_port,
            telemetry_otlp_port=settings.observability.telemetry_otlp_port,
            gateway=settings.gateway.gateway_url,
        ),
    )
    gateway_url, role, _desc, stopped_at = read_machine("station-a")
    assert gateway_url == "http://10.0.0.9:4318"
    assert role == ["observability-station"]
    assert stopped_at is None


def test_register_self_co_located_station_composes_with_gateway(
    machine_setup: MachineSetup, database_gate: ProcessDbGate
) -> None:
    machine_setup(name="combo", role="gateway", home="~/.meiva_gateway")
    machine_setup.register(machines_test_db(database_gate=database_gate), url="http://gw:8000")
    machine_setup(name="combo", role="observability-station", home="~/.meiva_station")
    machine_setup.register(machines_test_db(database_gate=database_gate), url=None)
    gateway_url, role, _desc, stopped_at = read_machine("combo")
    assert role == ["gateway", "observability-station"]
    assert gateway_url == "http://gw:8000"
    assert stopped_at is None


def _read_stopped_at(name: str):
    with psycopg.connect(settings.data_plane.db_url) as conn, conn.cursor() as cur:
        cur.execute("SELECT stopped_at FROM machines WHERE name = %s", (name,))
        row = cur.fetchone()
    return row[0] if row else None


def test_mark_stopping_stamps_then_register_clears(
    machine_setup: MachineSetup, database_gate: ProcessDbGate
) -> None:
    machine_setup(name="test-stopping", role="agent-runner", home="~/.meiva")
    machine_setup.register(machines_test_db(database_gate=database_gate), url=None)
    assert _read_stopped_at("test-stopping") is None

    machines.mark_stopping(
        machines_test_db(database_gate=database_gate), "test-stopping", "~/.meiva"
    )
    assert _read_stopped_at("test-stopping") is not None

    machine_setup.register(machines_test_db(database_gate=database_gate), url=None)
    assert _read_stopped_at("test-stopping") is None


def test_register_self_does_not_fall_back_to_gateway_url_when_url_none(
    machine_setup: MachineSetup, monkeypatch: pytest.MonkeyPatch, database_gate: ProcessDbGate
) -> None:
    machine_setup(name="test-no-fallback", role="agent-runner")
    monkeypatch.setattr(settings.gateway, "gateway_url", "http://should-be-ignored:8000")
    machine_setup.register(machines_test_db(database_gate=database_gate), url=None)
    assert machines.list_all(machines_test_db(database_gate=database_gate)) == [
        ("test-no-fallback", None)
    ]


def test_lookup_raises_when_missing(database_gate: ProcessDbGate) -> None:
    with pytest.raises(MachineNotRegistered):
        machines.lookup(machines_test_db(database_gate=database_gate), "never-registered")


def test_list_all_returns_registered(
    machine_setup: MachineSetup, database_gate: ProcessDbGate
) -> None:
    machine_setup(name="alpha")
    machine_setup.register(machines_test_db(database_gate=database_gate), url="http://a:8000")
    machine_setup(name="beta", role="agent-runner")
    machine_setup.register(machines_test_db(database_gate=database_gate), url=None)
    assert machines.list_all(machines_test_db(database_gate=database_gate)) == [
        ("alpha", "http://a:8000"),
        ("beta", None),
    ]


def test_list_agent_runners_filters_by_role(
    machine_setup: MachineSetup, database_gate: ProcessDbGate
) -> None:
    machine_setup(name="cp", role="gateway")
    machine_setup.register(machines_test_db(database_gate=database_gate), url="http://cp:8000")
    machine_setup(name="host-b", role="agent-runner")
    machine_setup.register(machines_test_db(database_gate=database_gate), url="http://b:9000")
    machine_setup(name="host-a", role="agent-runner")
    machine_setup.register(machines_test_db(database_gate=database_gate), url=None)
    assert machines.list_agent_runners(machines_test_db(database_gate=database_gate)) == [
        ("host-a", None),
        ("host-b", "http://b:9000"),
    ]


def test_list_agent_runners_includes_all_runners(
    machine_setup: MachineSetup, database_gate: ProcessDbGate
) -> None:
    machine_setup(name="other-box", role="agent-runner")
    machine_setup.register(machines_test_db(database_gate=database_gate), url="http://other:9000")
    machine_setup(name="this-box", role="agent-runner")
    machine_setup.register(machines_test_db(database_gate=database_gate), url="http://local:9000")

    assert machines.list_agent_runners(machines_test_db(database_gate=database_gate)) == [
        ("other-box", "http://other:9000"),
        ("this-box", "http://local:9000"),
    ]


def test_list_agent_runners_excludes_intentionally_stopped(
    machine_setup: MachineSetup, database_gate: ProcessDbGate
) -> None:
    machine_setup(name="running", role="agent-runner", home="~/.meiva")
    machine_setup.register(machines_test_db(database_gate=database_gate), url="http://running:9000")
    machine_setup(name="stopped", role="agent-runner", home="~/.meiva")
    machine_setup.register(machines_test_db(database_gate=database_gate), url="http://stopped:9000")
    machines.mark_stopping(machines_test_db(database_gate=database_gate), "stopped", "~/.meiva")

    assert machines.list_agent_runners(machines_test_db(database_gate=database_gate)) == [
        ("running", "http://running:9000")
    ]


def test_list_stopped_agent_runners_is_the_exact_complement(
    machine_setup: MachineSetup, database_gate: ProcessDbGate
) -> None:
    machine_setup(name="running", role="agent-runner", home="~/.meiva")
    machine_setup.register(machines_test_db(database_gate=database_gate), url="http://running:9000")
    machine_setup(name="stopped", role="agent-runner", home="~/.meiva")
    machine_setup.register(machines_test_db(database_gate=database_gate), url="http://stopped:9000")
    machines.mark_stopping(machines_test_db(database_gate=database_gate), "stopped", "~/.meiva")

    assert machines.list_stopped_agent_runners(machines_test_db(database_gate=database_gate)) == [
        ("stopped", "http://stopped:9000")
    ]
    assert (
        set(machines.list_agent_runners(machines_test_db(database_gate=database_gate)))
        & set(machines.list_stopped_agent_runners(machines_test_db(database_gate=database_gate)))
        == set()
    )


def test_list_agent_runners_excludes_staging(
    machine_setup: MachineSetup, database_gate: ProcessDbGate
) -> None:
    machine_setup(name="prod", role="agent-runner", home="~/.meiva")
    machine_setup.register(machines_test_db(database_gate=database_gate), url="http://prod:9000")
    machine_setup(name="stage", role="agent-runner", home="~/.meiva")
    machine_setup.register(machines_test_db(database_gate=database_gate), url="http://stage:9000")

    assert (
        machines.set_staging(
            machines_test_db(database_gate=database_gate), "stage", is_staging=True
        )
        is True
    )
    assert machines.list_agent_runners(machines_test_db(database_gate=database_gate)) == [
        ("prod", "http://prod:9000")
    ]
    assert machines.list_stopped_agent_runners(machines_test_db(database_gate=database_gate)) == []

    machine_setup.register(machines_test_db(database_gate=database_gate), url="http://stage:9000")
    assert machines.list_agent_runners(machines_test_db(database_gate=database_gate)) == [
        ("prod", "http://prod:9000")
    ]

    assert (
        machines.set_staging(
            machines_test_db(database_gate=database_gate), "stage", is_staging=False
        )
        is True
    )
    assert machines.list_agent_runners(machines_test_db(database_gate=database_gate)) == [
        ("prod", "http://prod:9000"),
        ("stage", "http://stage:9000"),
    ]


def test_set_staging_unknown_machine_returns_false(
    machine_setup: MachineSetup, database_gate: ProcessDbGate
) -> None:
    assert (
        machines.set_staging(
            machines_test_db(database_gate=database_gate), "ghost", is_staging=True
        )
        is False
    )


def _read_pause(name: str) -> tuple:
    with psycopg.connect(settings.data_plane.db_url) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT paused_at, pause_reason FROM machines WHERE name = %s",
            (name,),
        )
        row = cur.fetchone()
    assert row is not None
    return row


def test_pause_sets_latch_and_excludes_from_fanout(
    machine_setup: MachineSetup, database_gate: ProcessDbGate
) -> None:
    machine_setup(name="prod", role="agent-runner", home="~/.meiva")
    machine_setup.register(machines_test_db(database_gate=database_gate), url="http://prod:9000")
    machine_setup(name="away", role="agent-runner", home="~/.meiva")
    machine_setup.register(machines_test_db(database_gate=database_gate), url="http://away:9000")

    assert (
        machines.pause(
            machines_test_db(database_gate=database_gate), "away", reason="\u4f11\u5047\u4e00\u5468"
        )
        is True
    )
    paused_at, pause_reason = _read_pause("away")
    assert paused_at is not None
    assert pause_reason == "\u4f11\u5047\u4e00\u5468"
    assert machines.list_agent_runners(machines_test_db(database_gate=database_gate)) == [
        ("prod", "http://prod:9000")
    ]
    assert machines.list_paused(machines_test_db(database_gate=database_gate)) == [
        ("away", "http://away:9000")
    ]
    assert machines.list_stopped_agent_runners(machines_test_db(database_gate=database_gate)) == []


def test_register_self_does_not_clear_pause(
    machine_setup: MachineSetup, database_gate: ProcessDbGate
) -> None:
    machine_setup(name="away", role="agent-runner", home="~/.meiva")
    machine_setup.register(machines_test_db(database_gate=database_gate), url="http://away:9000")
    machines.pause(machines_test_db(database_gate=database_gate), "away", reason="away")

    machine_setup.register(
        machines_test_db(database_gate=database_gate), url="http://away-new-ip:9000"
    )

    paused_at, pause_reason = _read_pause("away")
    assert paused_at is not None
    assert pause_reason == "away"
    assert machines.list_agent_runners(machines_test_db(database_gate=database_gate)) == []
    assert (
        machines.lookup(machines_test_db(database_gate=database_gate), "away")
        == "http://away-new-ip:9000"
    )


def test_resume_clears_latch_and_restores_fanout(
    machine_setup: MachineSetup, database_gate: ProcessDbGate
) -> None:
    machine_setup(name="away", role="agent-runner", home="~/.meiva")
    machine_setup.register(machines_test_db(database_gate=database_gate), url="http://away:9000")
    machines.pause(machines_test_db(database_gate=database_gate), "away", reason="away")

    assert machines.resume(machines_test_db(database_gate=database_gate), "away") is True
    assert _read_pause("away") == (None, None)
    assert machines.list_agent_runners(machines_test_db(database_gate=database_gate)) == [
        ("away", "http://away:9000")
    ]
    assert machines.list_paused(machines_test_db(database_gate=database_gate)) == []


def test_pause_resume_idempotency(
    machine_setup: MachineSetup, database_gate: ProcessDbGate
) -> None:
    machine_setup(name="away", role="agent-runner", home="~/.meiva")
    machine_setup.register(machines_test_db(database_gate=database_gate), url="http://away:9000")

    assert machines.pause(machines_test_db(database_gate=database_gate), "away") is True
    assert (
        machines.pause(
            machines_test_db(database_gate=database_gate), "away", reason="second attempt"
        )
        is False
    )
    assert _read_pause("away")[1] is None

    assert machines.resume(machines_test_db(database_gate=database_gate), "away") is True
    assert machines.resume(machines_test_db(database_gate=database_gate), "away") is False


def test_pause_unknown_machine_returns_false(
    machine_setup: MachineSetup, database_gate: ProcessDbGate
) -> None:
    assert (
        machines.pause(machines_test_db(database_gate=database_gate), "ghost", reason="x") is False
    )
    assert machines.resume(machines_test_db(database_gate=database_gate), "ghost") is False


def test_is_paused_reads_the_latch(
    machine_setup: MachineSetup, database_gate: ProcessDbGate
) -> None:
    machine_setup(name="away", role="agent-runner", home="~/.meiva")
    machine_setup.register(machines_test_db(database_gate=database_gate), url="http://away:9000")

    assert machines.is_paused(machines_test_db(database_gate=database_gate), "away") is False
    machines.pause(machines_test_db(database_gate=database_gate), "away")
    assert machines.is_paused(machines_test_db(database_gate=database_gate), "away") is True
    machines.resume(machines_test_db(database_gate=database_gate), "away")
    assert machines.is_paused(machines_test_db(database_gate=database_gate), "away") is False
    with pytest.raises(MachineNotRegistered):
        machines.is_paused(machines_test_db(database_gate=database_gate), "never-registered")


def test_pause_independent_of_staging_and_stop(
    machine_setup: MachineSetup, database_gate: ProcessDbGate
) -> None:
    machine_setup(name="prod", role="agent-runner", home="~/.meiva")
    machine_setup.register(machines_test_db(database_gate=database_gate), url="http://prod:9000")
    machine_setup(name="away", role="agent-runner", home="~/.meiva")
    machine_setup.register(machines_test_db(database_gate=database_gate), url="http://away:9000")

    machines.set_staging(machines_test_db(database_gate=database_gate), "away", is_staging=True)
    machines.pause(machines_test_db(database_gate=database_gate), "away")
    assert machines.list_agent_runners(machines_test_db(database_gate=database_gate)) == [
        ("prod", "http://prod:9000")
    ]
    machines.set_staging(machines_test_db(database_gate=database_gate), "away", is_staging=False)
    assert machines.list_agent_runners(machines_test_db(database_gate=database_gate)) == [
        ("prod", "http://prod:9000")
    ]
    assert machines.list_stopped_agent_runners(machines_test_db(database_gate=database_gate)) == []
    machines.resume(machines_test_db(database_gate=database_gate), "away")
    assert machines.list_agent_runners(machines_test_db(database_gate=database_gate)) == [
        ("away", "http://away:9000"),
        ("prod", "http://prod:9000"),
    ]


def test_colocated_units_compose_union_and_ops_dial(
    machine_setup: MachineSetup, database_gate: ProcessDbGate
) -> None:
    machine_setup(name="test-host", role="gateway", home="~/.meiva_gateway")
    machine_setup.register(
        machines_test_db(database_gate=database_gate), url="http://test-host:8000"
    )
    machine_setup(name="test-host", role="agent-runner", home="~/.meiva")
    machine_setup.register(
        machines_test_db(database_gate=database_gate), url="http://localhost:8600"
    )

    gateway_url, role, _desc, stopped_at = read_machine("test-host")
    assert role == ["agent-runner", "gateway"]
    assert gateway_url == "http://localhost:8600"
    assert stopped_at is None
    assert machines.list_all(machines_test_db(database_gate=database_gate)) == [
        ("test-host", "http://localhost:8600")
    ]


def test_colocated_stop_one_unit_retracts_only_its_caps(
    machine_setup: MachineSetup, database_gate: ProcessDbGate
) -> None:
    machine_setup(name="test-host", role="gateway", home="~/.meiva_gateway")
    machine_setup.register(
        machines_test_db(database_gate=database_gate), url="http://test-host:8000"
    )
    machine_setup(name="test-host", role="agent-runner", home="~/.meiva")
    machine_setup.register(
        machines_test_db(database_gate=database_gate), url="http://localhost:8600"
    )

    machines.mark_stopping(machines_test_db(database_gate=database_gate), "test-host", "~/.meiva")

    gateway_url, role, _desc, stopped_at = read_machine("test-host")
    assert role == ["gateway"]
    assert gateway_url == "http://test-host:8000"
    assert stopped_at is None
    assert machines.list_agent_runners(machines_test_db(database_gate=database_gate)) == []


def test_colocated_restart_unit_recomposes_union(
    machine_setup: MachineSetup, database_gate: ProcessDbGate
) -> None:
    machine_setup(name="test-host", role="gateway", home="~/.meiva_gateway")
    machine_setup.register(
        machines_test_db(database_gate=database_gate), url="http://test-host:8000"
    )
    machine_setup(name="test-host", role="agent-runner", home="~/.meiva")
    machine_setup.register(
        machines_test_db(database_gate=database_gate), url="http://localhost:8600"
    )
    machines.mark_stopping(machines_test_db(database_gate=database_gate), "test-host", "~/.meiva")

    machine_setup(name="test-host", role="agent-runner", home="~/.meiva")
    machine_setup.register(
        machines_test_db(database_gate=database_gate), url="http://localhost:8600"
    )

    _gateway_url, role, _desc, _stopped = read_machine("test-host")
    assert role == ["agent-runner", "gateway"]


def test_register_self_rejects_loopback_ops_url_when_gateway_remote(
    machine_setup: MachineSetup, monkeypatch: pytest.MonkeyPatch, database_gate: ProcessDbGate
) -> None:
    machine_setup(name="runner-a", role="agent-runner")
    monkeypatch.setattr(settings.gateway, "gateway_url", "https://gw.example.com:8000")
    with pytest.raises(machines.LoopbackDialUrlRefused):
        machine_setup.register(
            machines_test_db(database_gate=database_gate), url="http://localhost:8600"
        )
    assert machines.list_all(machines_test_db(database_gate=database_gate)) == []


def test_register_self_rejects_loopback_station_url_when_gateway_remote(
    machine_setup: MachineSetup, monkeypatch: pytest.MonkeyPatch, database_gate: ProcessDbGate
) -> None:
    machine_setup(name="station-a", role="observability-station")
    monkeypatch.setattr(settings.gateway, "gateway_url", "https://gw.example.com:8000")
    with pytest.raises(machines.LoopbackDialUrlRefused):
        machine_setup.register(
            machines_test_db(database_gate=database_gate), url="http://localhost:4318"
        )
    assert machines.list_all(machines_test_db(database_gate=database_gate)) == []


def test_register_self_allows_loopback_station_url_when_gateway_colocated(
    machine_setup: MachineSetup, database_gate: ProcessDbGate
) -> None:
    machine_setup(name="station-local", role="observability-station")
    machine_setup.register(
        machines_test_db(database_gate=database_gate), url="http://localhost:4318"
    )
    gateway_url, role, _desc, _stopped = read_machine("station-local")
    assert gateway_url == "http://localhost:4318"
    assert role == ["observability-station"]


def test_register_self_allows_loopback_ops_url_when_gateway_colocated(
    machine_setup: MachineSetup, monkeypatch: pytest.MonkeyPatch, database_gate: ProcessDbGate
) -> None:
    machine_setup(name="box", role="agent-runner")
    monkeypatch.setattr(settings.gateway, "gateway_url", "http://localhost:8000")
    machine_setup.register(
        machines_test_db(database_gate=database_gate), url="http://localhost:8600"
    )
    assert machines.list_all(machines_test_db(database_gate=database_gate)) == [
        ("box", "http://localhost:8600")
    ]


def test_register_self_allows_loopback_ops_url_when_also_gateway(
    machine_setup: MachineSetup, monkeypatch: pytest.MonkeyPatch, database_gate: ProcessDbGate
) -> None:
    machine_setup(name="single", role="gateway,agent-runner")
    monkeypatch.setattr(settings.gateway, "gateway_url", "https://gw.example.com:8000")
    machine_setup.register(
        machines_test_db(database_gate=database_gate), url="http://localhost:8600"
    )
    assert machines.list_all(machines_test_db(database_gate=database_gate)) == [
        ("single", "http://localhost:8600")
    ]


def test_register_self_writes_description(
    machine_setup: MachineSetup, database_gate: ProcessDbGate
) -> None:

    machine_setup(name="desc-machine")
    machine_setup.machine.set(description="voice IO + browser")
    machine_setup.register(machines_test_db(database_gate=database_gate), url="http://d:8000")
    assert read_description("desc-machine") == "voice IO + browser"


def test_register_self_description_none_stores_null(
    machine_setup: MachineSetup, database_gate: ProcessDbGate
) -> None:

    machine_setup(name="nodesc-machine")
    machine_setup.machine.set(description=None)
    machine_setup.register(machines_test_db(database_gate=database_gate), url="http://n:8000")
    assert read_description("nodesc-machine") is None


def test_register_self_updates_description_on_conflict(
    machine_setup: MachineSetup, database_gate: ProcessDbGate
) -> None:

    machine_setup(name="upd-machine")
    machine_setup.machine.set(description="old")
    machine_setup.register(machines_test_db(database_gate=database_gate), url="http://u:8000")
    machine_setup.machine.set(description="new")
    machine_setup.register(machines_test_db(database_gate=database_gate), url="http://u:8000")
    assert read_description("upd-machine") == "new"
