"""Gateway entry facts and owned admission remain independent of later checkout moves."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

from base.config import ConfigBoot
from base.db import Database
from base.db.code_version_gate import ProcessDbGate
from base.db.config import DbConfig
from base.host import proc
from base.native_process.loaded_commit import LoadedCommit
from base.telemetry import DrainStatus
from gateway import app as gateway_app
from gateway.cluster import server
from gateway.cluster.process_boot import LOADED_IMAGE, GatewayProcess
from tests.fixtures.configuration import snapshot_process_config
from tests.fixtures.gateway_config import gateway_test_client


def test_both_gateway_entries_use_the_same_first_load_fact() -> None:
    assert server.LOADED_IMAGE is LOADED_IMAGE
    assert gateway_app.LOADED_IMAGE is LOADED_IMAGE


def test_gateway_owner_construction_is_cold(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(_boot: ConfigBoot, _name: str) -> object:
        raise AssertionError("entry owner read configuration during construction")

    config = ConfigBoot()
    monkeypatch.setattr(ConfigBoot, "get_field", forbidden)
    process = GatewayProcess(config=config, image=LoadedCommit(Path("unknown-source"), None))
    with process.lifetime():
        assert process.gate.min_read_due()
        assert process.clients.sync_events().status is DrainStatus.COMPLETED


def test_gateway_handles_retain_the_same_entry_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    gates: list[ProcessDbGate] = []
    original = Database.__init__

    def build(handle: Database, config: DbConfig, *, gate: ProcessDbGate) -> None:
        gates.append(gate)
        original(handle, config, gate=gate)

    process = GatewayProcess(config=snapshot_process_config(), image=LOADED_IMAGE)
    monkeypatch.setattr(Database, "__init__", build)
    with process.lifetime():
        first = process.database()
        second = process.database()
        assert first is not second
    assert gates == [process.gate, process.gate]


def test_gateway_cleanup_preserves_a_startup_primary(monkeypatch: pytest.MonkeyPatch) -> None:
    from base.agents.context.clients import ClientSet

    primary = ValueError("startup failed")
    cleanup = RuntimeError("writer close failed")

    def fail(_clients: ClientSet) -> None:
        raise cleanup

    process = GatewayProcess(config=ConfigBoot(), image=LOADED_IMAGE)
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
    script = r"""
import json, subprocess, sys
from pathlib import Path
sys.path[:0] = [sys.argv[1], sys.argv[2]]
from gateway.cluster.process_boot import LOADED_IMAGE
from base.native_process.code_version import CodeVersion
root = Path(sys.argv[1])
first = LOADED_IMAGE.sha
(root / "moved").write_text("new checkout")
subprocess.run(["git", "-C", str(root), "add", "."], check=True)
subprocess.run(["git", "-C", str(root), "-c", "user.name=entry-proof", "-c", "user.email=proof@example.invalid", "commit", "-qm", "second"], check=True)
from gateway.cluster.process_boot import LOADED_IMAGE as repeated
current = subprocess.check_output(["git", "-C", str(root), "rev-parse", "HEAD"], text=True).strip()
print(json.dumps({"first": first, "after": repeated.sha, "same": repeated is LOADED_IMAGE, "current": current, "version": CodeVersion(LOADED_IMAGE).get()}))
"""
    result = proc.run_bounded(
        [sys.executable, "-c", script, str(source), str(Path(__file__).resolve().parents[3])],
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
    monkeypatch: pytest.MonkeyPatch, sha: str | None, database: Database
) -> None:
    # Health image rendering uses this explicit native test database owner;
    # admission refusal for an unknown image has its own gate contract.
    def test_database(_process: GatewayProcess) -> Database:
        return database

    monkeypatch.setattr(GatewayProcess, "database", test_database)
    image = LoadedCommit(Path("entry-source"), sha)
    process = GatewayProcess(config=snapshot_process_config(), image=image)
    monkeypatch.setattr(gateway_app.app.state, "gateway_process_input", process, raising=False)
    with process.lifetime(), gateway_test_client(gateway_app.app) as client:
        first = client.get("/api/health")
        second = client.get("/api/health")
        assert first.status_code == second.status_code == 200
        assert first.json()["sha"] == second.json()["sha"] == sha
        assert gateway_app.app.state.gateway_process is process
        assert gateway_app.app.state.process_image is image
