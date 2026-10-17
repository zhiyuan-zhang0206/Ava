from __future__ import annotations

import ast
import json
import os
import sys
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from base import paths
from base.cluster.dataplane.direct_url import ClusterDirectUrl
from base.cluster.machine import MachineIdentity
from base.cluster.machine.boot import machine_from_boot
from base.cluster.machine.tests.identity import machine_identity as machine_identity
from base.config import ConfigBoot
from base.config.service_read import ConfigAuthority
from base.config.tests.configuration import snapshot_process_config
from base.db import Database
from base.db.code_version_gate import ProcessDbGate
from base.db.config import DbConfig
from base.deploy.maintenance import pause_owner
from base.deploy.maintenance.state import MaintenanceHold, MaintenancePhase
from base.host import proc
from base.host.net.auth import bearer_header, cookie_name
from base.host.system.tests.fake_commands import fake_host
from base.native_process.loaded_commit import LoadedCommit
from base.paths import meiva_home
from base.telemetry import DrainStatus
from gateway import app as gateway_app
from gateway.cluster import server
from gateway.cluster.process_boot import LOADED_IMAGE, GatewayProcess
from gateway.http.config import gateway_config_from_boot
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
        home=meiva_home(),
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
        home=meiva_home(),
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
        home=meiva_home(),
        config=ConfigBoot(),
        image=LOADED_IMAGE,
        machine=machine_identity,
        host=fake_host(),
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
    env["MEIVA_HOME"] = str(tmp_path / "home")
    env["MEIVA_CONFIG_FETCH"] = "skip"
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
        home=meiva_home(),
        config=snapshot_process_config(),
        image=image,
        machine=machine_identity,
        host=fake_host(),
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
        "MEIVA_MACHINE_SERVE_GATEWAY=true\n"
        f"MEIVA_DB_URL={config_authority.service_field_value('db_url')}\n"
        f"MEIVA_REDIS_URL={config_authority.service_field_value('redis_url')}\n",
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


_ROOT_READS = frozenset({"meiva_home", "resolve_meiva_home"})
_WHEN = datetime(2026, 10, 17, tzinfo=UTC)


def _rooted_app(home: Path) -> Any:
    boot = snapshot_process_config()
    process = GatewayProcess(
        home=home,
        config=boot,
        image=LOADED_IMAGE,
        machine=machine_from_boot(boot),
        host=fake_host(),
    )
    return gateway_app.create_app(
        gateway_config_from_boot(process.config), process=process, process_ownership="owned"
    )


def _second_root(first: Path) -> Path:
    home = first / "second-root"
    home.mkdir(mode=0o700)
    return home


def _hold(home: Path, phase: MaintenancePhase) -> None:
    pause_owner.write_atomic(
        pause_owner.state_path_for(home),
        {
            "state": "paused",
            "holder": "process-boot-test",
            "acquired_at": _WHEN.isoformat(),
            "maintenance": MaintenanceHold(phase).encode(),
        },
    )


def test_a_second_root_reads_none_of_the_first_roots_credentials_or_session(
    config_authority: ConfigAuthority, seed_write_generation: Callable[[Path], Any]
) -> None:
    first = config_authority.env_path.parent
    assert paths.meiva_home() == first
    second = _second_root(first)
    tokens = seed_write_generation(first).api

    with gateway_test_client(_rooted_app(first)) as client:
        assert client.get("/api/agents", headers=bearer_header(tokens.runner)).status_code == 200
        cookie = client.post("/api/auth/login", json={"password": tokens.runner}).cookies[
            cookie_name()
        ]
        assert cookie

    presented = {"Cookie": f"{cookie_name()}={cookie}"}
    with gateway_test_client(_rooted_app(second)) as other:
        assert other.get("/api/agents", headers=bearer_header(tokens.runner)).status_code == 401
        assert other.get("/api/auth/check", headers=presented).json() == {"authenticated": False}
        assert other.get("/api/agents").status_code == 200


def test_a_paused_root_does_not_fence_an_app_serving_another_root(
    config_authority: ConfigAuthority,
) -> None:
    first = config_authority.env_path.parent
    second = _second_root(first)
    _hold(first, MaintenancePhase.STOPPED)

    with gateway_test_client(_rooted_app(first)) as stopped:
        assert stopped.get("/api/agents").status_code == 503
        assert stopped.get("/api/health").status_code == 200
    with gateway_test_client(_rooted_app(second)) as client:
        assert client.get("/api/agents").status_code == 200


def test_an_app_serves_the_config_file_of_the_root_it_serves(
    config_authority: ConfigAuthority,
) -> None:
    first = config_authority.env_path.parent
    second = _second_root(first)
    (first / ".env").write_text(
        "MEIVA_DB_URL=postgresql://meiva_first@10.9.9.1:5432/meiva\n", encoding="utf-8"
    )
    (second / ".env").write_text(
        "MEIVA_DB_URL=postgresql://meiva_second@10.9.9.2:5432/meiva\n", encoding="utf-8"
    )

    with gateway_test_client(_rooted_app(first)) as client:
        assert "10.9.9.1" in client.get("/api/bootstrap").json()["MEIVA_DB_URL"]
    with gateway_test_client(_rooted_app(second)) as other:
        assert "10.9.9.2" in other.get("/api/bootstrap").json()["MEIVA_DB_URL"]


def test_requests_read_the_pause_journal_as_it_changes(config_authority: ConfigAuthority) -> None:
    first = config_authority.env_path.parent
    with gateway_test_client(_rooted_app(first)) as client:
        assert client.get("/api/agents").status_code == 200
        _hold(first, MaintenancePhase.STOPPED)
        assert client.get("/api/agents").status_code == 503
        _hold(first, MaintenancePhase.DRAINING)
        assert client.get("/api/agents").status_code == 200


def test_serving_an_app_adds_no_environment_slot(config_authority: ConfigAuthority) -> None:
    second = _second_root(config_authority.env_path.parent)
    before = set(os.environ)
    with gateway_test_client(_rooted_app(second)) as client:
        assert client.get("/api/health").status_code == 200
    assert set(os.environ) == before


def _root_read_calls(node: ast.AST) -> list[ast.Call]:
    return [
        inner
        for inner in ast.walk(node)
        if isinstance(inner, ast.Call) and _names_root_read(inner.func)
    ]


def _names_root_read(func: ast.expr) -> bool:
    name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
    return name in _ROOT_READS


def _resolving_functions(tree: ast.Module) -> dict[str, int]:
    per_function: dict[str, int] = {}

    def walk(node: ast.AST) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                calls = _root_read_calls(child)
                if calls:
                    per_function[child.name] = len(calls)
                walk(child)
                continue
            walk(child)

    walk(tree)
    return per_function


def test_the_data_root_is_resolved_only_at_the_gateway_entries() -> None:
    package = Path(paths.repo_root()) / "gateway"
    found = {
        str(path.relative_to(Path(paths.repo_root()))): _resolving_functions(
            ast.parse(path.read_text(encoding="utf-8"))
        )
        for path in sorted(package.rglob("*.py"))
        if "tests" not in path.relative_to(package).parts
        and "__pycache__" not in path.parts
        and not path.name.startswith("test_")
    }
    assert {module: functions for module, functions in found.items() if functions} == {
        "gateway/app.py": {"create_app_from_environment": 1, "openapi_schema": 1},
        "gateway/cluster/server.py": {"main": 1},
    }
