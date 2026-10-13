"""Invalid entry configuration fails before publishing memory-indexer health."""

from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from base.cluster.machine import MachineIdentity
from base.config import ConfigBoot
from base.db import Database
from base.native_process.loaded_commit import LoadedCommit
from services.derived.memory_indexer import daemon
from services.derived.memory_search.tests.memory_indexer.test_reconcile_embed_error_truncates_and_returns_false import (
    owned_config_environment as owned_config_environment,
)
from tests.fixtures.unit.identity import machine_identity as machine_identity


@pytest.mark.usefixtures("owned_config_environment")
async def test_run_unknown_provider_fails_before_health_server(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    database: Database,
    *,
    machine_identity: MachineIdentity,
    tmp_path: Path,
) -> None:
    start_health_server = AsyncMock()

    def is_running_for_entry(config: ConfigBoot, *, runtime: Path) -> bool:
        return False

    monkeypatch.setattr(daemon, "_is_running", is_running_for_entry)

    def write_pidfile_for_entry(config: ConfigBoot, *, runtime: Path) -> None:
        pass

    monkeypatch.setattr(daemon, "write_pidfile", write_pidfile_for_entry)
    monkeypatch.setattr(daemon, "start_health_server", start_health_server)
    boot = ConfigBoot()
    monkeypatch.setattr(boot.view.services, "embedding_backend", "unknown-provider")

    with pytest.raises(SystemExit) as exc:
        await daemon.run(
            runtime=tmp_path / "run",
            config=boot,
            database=lambda: database,
            image=LoadedCommit(Path(), None),
            machine=machine_identity,
        )

    assert exc.value.code == 1
    assert "FATAL: unknown embedding provider 'unknown-provider'" in capsys.readouterr().err
    start_health_server.assert_not_awaited()
