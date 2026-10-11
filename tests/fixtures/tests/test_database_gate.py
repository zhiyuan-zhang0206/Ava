"""Native test builders share one explicit, nonexempt loaded-image admission owner."""

from pathlib import Path
from typing import Any

import pytest

from base.config import settings
from base.db import Database, connections
from base.db.code_version_gate import ProcessDbGate
from base.native_process import code_version
from base.native_process.loaded_commit import LoadedCommit
from base.telemetry import process_name


def test_gate_is_lazy_and_shared_by_native_builders(
    request: pytest.FixtureRequest,
    monkeypatch: pytest.MonkeyPatch,
    database_version: code_version.CodeVersion,
    pytestconfig: pytest.Config,
) -> None:
    original = database_version.loaded
    assert original.source_root == pytestconfig.rootpath
    version = database_version.get()

    def unexpected_read(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("native session already owns its captured version")

    monkeypatch.setattr(LoadedCommit, "capture", unexpected_read)
    monkeypatch.setattr(code_version, "first_parent_count", unexpected_read)
    owner: ProcessDbGate = request.getfixturevalue("database_gate")
    assert owner.min_read_due()
    handles = [Database.from_settings(gate=owner), request.getfixturevalue("database")]
    seen: list[ProcessDbGate] = []

    def connect(*, config: Any, gate: ProcessDbGate, **_kwargs: Any) -> Any:
        assert config.db_url == settings.data_plane.db_url
        assert gate.application_name() == f"ava:{process_name()}:v{version}"
        if gate.min_read_due():
            gate.observe_minimum(version)
        seen.append(gate)
        return object()

    monkeypatch.setattr(connections, "connect", connect)
    for handle in handles:
        handle.connect()
    assert seen == [owner, owner]
    assert not owner.min_read_due()
    assert database_version.loaded is original


def test_gate_keeps_a_fresh_budget_for_each_test(
    request: pytest.FixtureRequest,
    database_version: code_version.CodeVersion,
) -> None:
    owner: ProcessDbGate = request.getfixturevalue("database_gate")
    assert owner.min_read_due()
    assert owner.application_name() == f"ava:{process_name()}:v{database_version.get()}"


def test_gate_preserves_an_unknown_nonexempt_session_image(tmp_path: Path) -> None:
    version = code_version.CodeVersion(LoadedCommit(tmp_path, None))
    owner = ProcessDbGate(version=version.get, process=process_name())
    assert owner.min_read_due()
    with pytest.raises(code_version.CodeVersionError, match="loaded commit"):
        owner.application_name()
    assert owner.min_read_due()
