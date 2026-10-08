"""Shared root-observation fixture owned by the serving lifecycle package."""

from pathlib import Path

import pytest

from base.deploy.lifecycle.start_serving import RootBirth


@pytest.fixture
def serving_root(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> RootBirth:
    """Explicit local-root observation double; excludes native/root proof.

    Maintenance/readiness unit tests retain the actual marker and locking code.
    Native IPC and loaded-origin contracts have independent real socket tests.
    """
    from base.deploy.lifecycle import start_serving
    from base.deploy.release.runtime_interpreter import LoadedRuntimeIdentity
    from base.native_process.evidence import ExpectedProcess

    runtime = LoadedRuntimeIdentity(
        kind="source",
        code_root=str(tmp_path),
        interpreter=str(tmp_path / "python"),
        prefix=str(tmp_path / "venv"),
        cwd=str(tmp_path),
        source_digest="a" * 64,
    )
    birth = start_serving.RootBirth(
        home=str(tmp_path),
        process=ExpectedProcess(pid=4321, create_time=123.0, starttime=456),
        launch_digest="b" * 64,
        runtime=runtime,
    )
    monkeypatch.setattr(start_serving, "_observe_root", lambda: birth)
    return birth
