"""A missing executable is not an unresolved child."""

import asyncio
from pathlib import Path

import pytest

from base.db import Database
from tests.fixtures.pin_agent import exec_context


async def test_real_missing_executable_is_not_an_unresolved_child(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, database: Database
) -> None:
    from agent.graph.exec._result import _ExecCrashed
    from agent.graph.exec._subprocess import _run_in_subprocess
    from base.native_process.turn_identity import HostedTurnResources

    monkeypatch.setattr("agent.graph.exec._subprocess.sys.executable", str(tmp_path / "absent"))
    scope = HostedTurnResources()
    outcome, _ = await _run_in_subprocess(
        database,
        "raise AssertionError('must never execute')",
        exec_context(None, resources=scope),
        asyncio.Event(),
        2,
        exec_dir=tmp_path,
        accumulation_max_chars=1_000_000,
    )
    assert isinstance(outcome, _ExecCrashed)
    assert "could not be spawned" in outcome.output
    assert not scope.unresolved
