"""Read-only, bounded detection of broken production virtualenvs."""

from __future__ import annotations

import logging
import subprocess
import venv
from pathlib import Path
from unittest.mock import Mock

import pytest

from services.healthchecks import prod_venv as hc


@pytest.fixture(autouse=True)
def _isolated_check(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(hc, "IS_WINDOWS", False)
    monkeypatch.setattr(hc, "init_gateway_process", Mock())
    monkeypatch.setattr(hc, "_reported_violations", ())
    monkeypatch.setattr(hc.cluster_drift, "prod_source_dir", lambda: tmp_path)
    interpreter = tmp_path / ".venv/bin/python"
    interpreter.parent.mkdir(parents=True)
    interpreter.touch()
    monkeypatch.setattr(hc.shutil, "which", Mock(return_value="/tools/uv"))


def _result(*, rc: int = 0, stderr: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess([], rc, stdout="", stderr=stderr)


def _messages(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [
        record.getMessage()
        for record in caplog.records
        if record.name == hc._log.name and record.levelno == logging.ERROR
    ]


def _package_files(site: Path) -> dict[Path, bytes]:
    return {p.relative_to(site): p.read_bytes() for p in site.rglob("*") if p.is_file()}


def test_healthy_is_silent_and_probes_the_explicit_venv(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv("VIRTUAL_ENV", "/unrelated/shared/.venv")
    monkeypatch.setenv("PYTHONPATH", "/unrelated/checkout")
    run = Mock(return_value=_result())
    monkeypatch.setattr(hc.proc, "run_bounded", run)

    hc.main()

    assert _messages(caplog) == []
    assert run.call_count == 2
    python = str(tmp_path / ".venv/bin/python")
    assert run.call_args_list[0].args[0] == ["/tools/uv", "pip", "check", "--python", python]
    assert run.call_args_list[1].args[0] == [python, "-I", "-B", "-c", hc._IMPORT_SMOKE]
    for call in run.call_args_list:
        assert "VIRTUAL_ENV" not in call.kwargs["env"]
        assert "PYTHONPATH" not in call.kwargs["env"]
        assert call.kwargs["timeout"] == 5.0
        assert call.kwargs["capture_output"] is True


@pytest.mark.parametrize(
    ("leg", "diagnostic"),
    [
        (0, "pydantic requires missing pydantic-core"),
        (1, "ModuleNotFoundError: No module named 'psycopg'"),
    ],
)
def test_failed_leg_reports_once_until_recovery_or_changed_failure(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    leg: int,
    diagnostic: str,
) -> None:
    results = [_result(), _result()]
    results[leg] = _result(rc=1, stderr=diagnostic)
    run = Mock(side_effect=results * 2)
    monkeypatch.setattr(hc.proc, "run_bounded", run)
    hc.main()
    hc.main()
    assert len(_messages(caplog)) == 1
    assert diagnostic in _messages(caplog)[0]
    assert _messages(caplog)[0].startswith("[prod-venv healthcheck]")

    results[leg] = _result(rc=1, stderr="ImportError: changed failure")
    run.side_effect = results
    hc.main()
    assert len(_messages(caplog)) == 2
    run.side_effect = [_result(), _result()]
    hc.main()
    assert hc._reported_violations == ()
    assert len(_messages(caplog)) == 2
    run.side_effect = results
    hc.main()
    assert len(_messages(caplog)) == 3


def test_missing_uv_skips_only_dependency_leg(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(hc.shutil, "which", Mock(return_value=None))
    run = Mock(return_value=_result())
    monkeypatch.setattr(hc.proc, "run_bounded", run)
    with caplog.at_level(logging.DEBUG, logger=hc._log.name):
        hc.main()
    assert run.call_count == 1
    assert "-c" in run.call_args.args[0]
    assert _messages(caplog) == []
    assert "uv not on PATH" in caplog.text

    run.return_value = _result(rc=1, stderr="ImportError: pydantic is hollow")
    hc.main()
    assert "pydantic is hollow" in _messages(caplog)[0]


def test_dependency_failure_deduplicates_despite_uv_timing_changes(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    diagnostic = "The package `pydantic` requires `pydantic-core`, but it's not installed"
    run = Mock(
        side_effect=[
            _result(rc=1, stderr=f"Checked 264 packages in 3ms\n{diagnostic}\n"),
            _result(),
            _result(rc=1, stderr=f"Checked 264 packages in 11ms\n{diagnostic}\n"),
            _result(),
        ]
    )
    monkeypatch.setattr(hc.proc, "run_bounded", run)
    hc.main()
    hc.main()
    assert len(_messages(caplog)) == 1
    assert diagnostic in _messages(caplog)[0]


@pytest.mark.parametrize(
    "error",
    [
        subprocess.TimeoutExpired("uv", 5, stderr=b"metadata stuck"),
        PermissionError("not executable"),
    ],
)
def test_dependency_probe_error_still_runs_imports(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, error: Exception
) -> None:
    run = Mock(side_effect=[error, _result(rc=1, stderr="ImportError: psycopg missing")])
    monkeypatch.setattr(hc.proc, "run_bounded", run)
    hc.main()
    assert run.call_count == 2
    assert len(_messages(caplog)) == 1
    message = _messages(caplog)[0]
    assert "uv pip check" in message
    assert "psycopg missing" in message
    assert (
        "metadata stuck" in message
        if isinstance(error, subprocess.TimeoutExpired)
        else "not executable" in message
    )


def test_error_diagnostics_are_tailed(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    run = Mock(
        side_effect=[_result(), _result(rc=1, stderr="discarded" + "x" * 2000 + "failure tail")]
    )
    monkeypatch.setattr(hc.proc, "run_bounded", run)
    hc.main()
    message = _messages(caplog)[0]
    assert "discarded" not in message
    assert "failure tail" in message
    assert len(message) < 1200


def test_missing_interpreter_reports_without_spawning(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    (tmp_path / ".venv/bin/python").unlink()
    run = Mock(side_effect=AssertionError("no interpreter to probe"))
    monkeypatch.setattr(hc.proc, "run_bounded", run)
    hc.main()
    assert "venv python missing" in _messages(caplog)[0]
    run.assert_not_called()


def test_windows_is_a_noop(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(hc, "IS_WINDOWS", True)
    source = Mock(side_effect=AssertionError("Windows must not resolve the POSIX venv"))
    monkeypatch.setattr(hc.cluster_drift, "prod_source_dir", source)
    hc.main()
    source.assert_not_called()


@pytest.mark.skipif(hc.IS_WINDOWS, reason="production venv healthcheck is POSIX-only")
@pytest.mark.parametrize("damage", ["healthy", "missing", "hollow"])
def test_real_isolated_import_detects_damage_without_writes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
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

    hc.main()

    assert before == _package_files(site)
    if damage == "healthy":
        assert _messages(caplog) == []
    else:
        message = _messages(caplog)[0]
        if damage == "missing":
            assert "No module named" in message
            assert "psycopg" in message
        else:
            assert "ava: hollow package" in message
