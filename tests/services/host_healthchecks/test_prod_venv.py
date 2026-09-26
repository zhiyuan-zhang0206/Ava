"""Bounded checkout dependency checks, including real isolated import evidence."""

from __future__ import annotations

import subprocess
import sys
import venv
from pathlib import Path
from unittest.mock import Mock

import pytest

from services.healthchecks import prod_venv as hc


@pytest.fixture(autouse=True)
def isolated(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(hc.cluster_drift, "prod_source_dir", lambda: tmp_path)
    interpreter = tmp_path / ".venv/bin/python"
    interpreter.parent.mkdir(parents=True)
    interpreter.touch()
    monkeypatch.setattr(hc.shutil, "which", Mock(return_value="/tools/uv"))


def _package_files(site: Path) -> dict[Path, bytes]:
    return {p.relative_to(site): p.read_bytes() for p in site.rglob("*") if p.is_file()}


def test_explicit_checkout_and_isolated_environment(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("VIRTUAL_ENV", "/other/.venv")
    monkeypatch.setenv("PYTHONPATH", "/other")
    run = Mock(return_value=subprocess.CompletedProcess([], 0, stdout="", stderr=""))
    monkeypatch.setattr(hc.proc, "run_bounded", run)
    assert hc._violations(source_root=tmp_path) == ()
    assert len(run.call_args_list) == 2
    assert run.call_args_list[0].args[0][-1] == str(tmp_path / ".venv/bin/python")
    for call in run.call_args_list:
        assert "VIRTUAL_ENV" not in call.kwargs["env"]
        assert "PYTHONPATH" not in call.kwargs["env"]
        assert call.kwargs["timeout"] == 5


def test_dependency_timeout_does_not_skip_import_check(monkeypatch: pytest.MonkeyPatch) -> None:
    run = Mock(
        side_effect=[
            subprocess.TimeoutExpired("uv", 5),
            subprocess.CompletedProcess([], 0, stdout="", stderr=""),
        ]
    )
    monkeypatch.setattr(hc.proc, "run_bounded", run)
    violations = hc._violations()
    assert len(violations) == 1
    assert "timed out" in violations[0]
    assert run.call_count == 2


def test_missing_python_does_not_spawn_a_diagnostic(monkeypatch: pytest.MonkeyPatch) -> None:
    def _no_python(_source_root: Path) -> Path | None:
        return None

    monkeypatch.setattr(hc.editable_install, "_venv_python", _no_python)
    run = Mock(side_effect=AssertionError("must not spawn"))
    monkeypatch.setattr(hc.proc, "run_bounded", run)
    assert "venv python missing" in hc._violations()[0]
    run.assert_not_called()


@pytest.mark.skipif(sys.platform == "win32", reason="production venv healthcheck is POSIX-only")
@pytest.mark.parametrize("damage", ["healthy", "missing", "hollow"])
def test_real_isolated_import_detects_damage_without_writes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    damage: str,
) -> None:
    # A disposable stdlib-only venv with four tiny packages. No production files
    # or uv invocation; the real child must discover its own site-packages.
    (tmp_path / ".venv/bin/python").unlink()
    venv.EnvBuilder(with_pip=False, symlinks=True).create(tmp_path / ".venv")
    site = next((tmp_path / ".venv/lib").glob("python*/site-packages"))
    for name in ("ava", "pydantic", "psycopg", "fastapi"):
        if name == "psycopg" and damage == "missing":
            continue
        package = site / name
        package.mkdir()
        if damage != "hollow":
            (package / "__init__.py").write_text("")
    # Even a good shadow package on PYTHONPATH must not hide the missing one.
    shadow = tmp_path / "shadow"
    shadow.mkdir()
    (shadow / "psycopg.py").write_text("")
    monkeypatch.setenv("PYTHONPATH", str(shadow))
    monkeypatch.setattr(hc.shutil, "which", Mock(return_value=None))
    before = _package_files(site)

    violations = hc._violations()

    assert before == _package_files(site)
    if damage == "healthy":
        assert violations == ()
    else:
        message = violations[0]
        if damage == "missing":
            assert "No module named" in message
            assert "psycopg" in message
        else:
            assert "ava: hollow package" in message
