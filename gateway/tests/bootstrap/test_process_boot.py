from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

from base.cluster.dataplane.direct_url import ClusterDirectUrl
from base.cluster.machine import MachineIdentity
from base.cluster.machine.tests.identity import machine_identity as machine_identity
from base.config import ConfigBoot
from base.config.service_read import ConfigAuthority
from base.config.tests.configuration import snapshot_process_config
from base.db import Database
from base.db.code_version_gate import ProcessDbGate
from base.db.config import DbConfig
from base.host import proc
from base.host.system.tests.fake_commands import fake_host
from base.native_process.loaded_commit import LoadedCommit
from base.telemetry import DrainStatus
from gateway import app as gateway_app
from gateway.cluster import server
from gateway.cluster.process_boot import LOADED_IMAGE, GatewayProcess
from tests.fixtures.gateway_config import gateway_test_app, gateway_test_client


def test_both_gateway_entries_use_the_same_first_load_fact() -> None:
    assert server.LOADED_IMAGE is LOADED_IMAGE
    assert gateway_app.LOADED_IMAGE is LOADED_IMAGE


def _forbid_config_reads(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(_boot: ConfigBoot, _name: str) -> object:
        raise AssertionError("entry owner read configuration during cold construction")

    monkeypatch.setattr(ConfigBoot, "get_field", forbidden)


def test_gateway_owner_construction_is_cold(
    monkeypatch: pytest.MonkeyPatch, *, machine_identity: MachineIdentity
) -> None:
    config = ConfigBoot()
    _forbid_config_reads(monkeypatch)
    process = GatewayProcess(
        config=config,
        image=LoadedCommit(Path("unknown-source"), None),
        machine=machine_identity,
        host=fake_host(),
    )
    with process.lifetime():
        assert process.gate.min_read_due()
        assert process.clients.sync_events().status is DrainStatus.COMPLETED


def test_gateway_handles_retain_the_same_entry_gate(
    monkeypatch: pytest.MonkeyPatch, *, machine_identity: MachineIdentity
) -> None:
    gates: list[ProcessDbGate] = []
    original = Database.__init__

    def build(
        handle: Database, config: DbConfig, *, gate: ProcessDbGate, direct_url: ClusterDirectUrl
    ) -> None:
        gates.append(gate)
        original(handle, config, gate=gate, direct_url=direct_url)

    process = GatewayProcess(
        config=snapshot_process_config(),
        image=LOADED_IMAGE,
        machine=machine_identity,
        host=fake_host(),
    )
    monkeypatch.setattr(Database, "__init__", build)
    with process.lifetime():
        first = process.database()
        second = process.database()
        assert first is not second
    assert gates == [process.gate, process.gate]


def test_gateway_cleanup_preserves_a_startup_primary(
    monkeypatch: pytest.MonkeyPatch, *, machine_identity: MachineIdentity
) -> None:
    from base.agents.context.clients import ClientSet

    primary = ValueError("startup failed")
    cleanup = RuntimeError("writer close failed")

    def fail(_clients: ClientSet) -> None:
        raise cleanup

    process = GatewayProcess(
        config=ConfigBoot(), image=LOADED_IMAGE, machine=machine_identity, host=fake_host()
    )
    monkeypatch.setattr(ClientSet, "close", fail)
    with pytest.raises(ValueError) as caught, process.lifetime():
        raise primary
    assert caught.value is primary
    assert any("writer close failed" in note for note in primary.__notes__)


def test_gateway_first_load_image_does_not_follow_a_checkout_move(tmp_path: Path) -> None:
    source = tmp_path / "source"
    module = source / "gateway" / "cluster"
    module.mkdir(parents=True)
    (source / "gateway" / "__init__.py").write_text("")
    (module / "__init__.py").write_text("")
    original = Path(server.__file__).with_name("process_boot.py")
    (module / "process_boot.py").write_text(original.read_text())
    for args in (
        ["init", "-q"],
        ["add", "."],
        [
            "-c",
            "user.name=entry-proof",
            "-c",
            "user.email=proof@example.invalid",
            "commit",
            "-qm",
            "first",
        ],
    ):
        result = proc.run_bounded(
            ["git", "-C", str(source), *args], timeout=10, capture_output=True
        )
        assert result.returncode == 0, result.stderr
    env = dict(os.environ)
    env["AVA_HOME"] = str(tmp_path / "home")
    env["AVA_CONFIG_FETCH"] = "skip"
    result = proc.run_bounded(
        [
            sys.executable,
            str(Path(__file__).with_name("child_gateway_scenarios.py")),
            "checkout_move",
            str(source),
            str(Path(__file__).resolve().parents[3]),
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    fact = json.loads(result.stdout)
    assert fact["same"] is True
    assert fact["first"] == fact["after"]
    assert fact["first"] != fact["current"]
    assert fact["version"] == 1


@pytest.mark.parametrize("sha", [None, "entry-image-sha"])
def test_asgi_health_keeps_its_explicit_entry_image(
    monkeypatch: pytest.MonkeyPatch,
    sha: str | None,
    database: Database,
    *,
    machine_identity: MachineIdentity,
) -> None:
    def test_database(_process: GatewayProcess) -> Database:
        return database

    monkeypatch.setattr(GatewayProcess, "database", test_database)
    image = LoadedCommit(Path("entry-source"), sha)
    process = GatewayProcess(
        config=snapshot_process_config(), image=image, machine=machine_identity, host=fake_host()
    )
    app = gateway_test_app(process=process)
    with (
        process.lifetime(),
        gateway_test_client(app) as client,
    ):
        first = client.get("/api/health")
        second = client.get("/api/health")
        assert first.status_code == second.status_code == 200
        assert first.json()["sha"] == second.json()["sha"] == sha
        assert app.state.gateway_process is process
        assert app.state.process_image is image


def test_openapi_schema_is_cold(monkeypatch: pytest.MonkeyPatch) -> None:
    _forbid_config_reads(monkeypatch)
    schema = gateway_app.openapi_schema()
    assert isinstance(schema, dict)
    assert "/api/health" in schema["paths"]


def test_create_app_from_environment_assembles_gateway_config(
    config_authority: ConfigAuthority,
) -> None:
    config_authority.env_path.write_text(
        "AVA_MACHINE_SERVE_GATEWAY=true\n"
        f"AVA_DB_URL={config_authority.service_field_value('db_url')}\n"
        f"AVA_REDIS_URL={config_authority.service_field_value('redis_url')}\n",
        encoding="utf-8",
    )
    saved_env = dict(os.environ)
    try:
        first = gateway_app.create_app_from_environment()
        second = gateway_app.create_app_from_environment()
        expected = gateway_app.gateway_config_from_boot(ConfigBoot())
    finally:
        os.environ.clear()
        os.environ.update(saved_env)
    assert first is not second
    assert first.state.gateway_config == expected
    assert first.state.machine_token_acceptance == {}
    assert first.state.machine_token_acceptance is not second.state.machine_token_acceptance
