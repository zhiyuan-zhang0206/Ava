"""Contract tests for runner-native visual baseline refreshes."""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest

from scripts import refresh_visual_baselines as refresh

RunResult = subprocess.CompletedProcess[str]


def _prepare_references(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, bytes]:
    monkeypatch.setattr(refresh, "_REPO_ROOT", tmp_path)
    originals: dict[str, bytes] = {}
    for test_name, relative_path in refresh._BASELINE_PATHS_BY_TEST.items():
        contents = f"old-{test_name}".encode()
        reference = tmp_path / relative_path
        reference.parent.mkdir(parents=True, exist_ok=True)
        reference.write_bytes(contents)
        originals[test_name] = contents
    return originals


def _completed(command: list[str], returncode: int, stdout: str = "") -> RunResult:
    return subprocess.CompletedProcess(command, returncode, stdout=stdout)


def _assert_run_options(options: dict[str, object], tmp_path: Path) -> None:
    assert options == {
        "cwd": tmp_path,
        "check": False,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.STDOUT,
        "text": True,
    }


def _drift_failure(test_name: str) -> str:
    return (
        f"FAILED tests/e2e/test_visual_regression.py::{test_name} - Failed: "
        "Visual regression: 1001/100000 pixels changed (1.001%); "
        "allowed ratio is 0.100%.\n"
    )


def _candidate_failures(tmp_path: Path) -> str:
    return "".join(
        f"FAILED tests/e2e/test_visual_regression.py::{test_name} - Failed: "
        f"Generated visual baseline candidate at {tmp_path / relative_path}. "
        "Review and commit it before rerunning the test.\n"
        for test_name, relative_path in refresh._BASELINE_PATHS_BY_TEST.items()
    )


def _install_runner(
    monkeypatch: pytest.MonkeyPatch,
    runner: Callable[..., RunResult],
) -> None:
    monkeypatch.setattr(refresh.subprocess, "run", runner)


def test_check_only_returns_success_when_visual_tests_pass(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(refresh, "_REPO_ROOT", tmp_path)

    def run(command: list[str], **options: object) -> RunResult:
        assert command == refresh._VISUAL_TEST_COMMAND
        _assert_run_options(options, tmp_path)
        return _completed(command, 0, "3 passed\n")

    _install_runner(monkeypatch, run)

    assert refresh.main(["--check-only"]) == 0


def test_check_only_maps_pixel_drift_to_distinct_exit_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(refresh, "_REPO_ROOT", tmp_path)

    def run(command: list[str], **options: object) -> RunResult:
        _assert_run_options(options, tmp_path)
        return _completed(command, 1, _drift_failure("test_home_visual_regression"))

    _install_runner(monkeypatch, run)

    assert refresh.main(["--check-only"]) == refresh.VISUAL_DRIFT_EXIT_CODE


@pytest.mark.parametrize("returncode", [1, 2, 3, 4, 5])
def test_check_only_propagates_non_drift_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, returncode: int
) -> None:
    monkeypatch.setattr(refresh, "_REPO_ROOT", tmp_path)

    def run(command: list[str], **options: object) -> RunResult:
        _assert_run_options(options, tmp_path)
        return _completed(command, returncode, "ERROR visual test setup failed\n")

    _install_runner(monkeypatch, run)

    assert refresh.main(["--check-only"]) == returncode


def test_check_only_does_not_mask_a_failure_mixed_with_pixel_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(refresh, "_REPO_ROOT", tmp_path)
    output = _drift_failure("test_home_visual_regression") + (
        "FAILED tests/e2e/test_visual_regression.py::test_fleet_visual_regression - "
        "playwright.sync_api.TimeoutError: page did not load\n"
    )

    def run(command: list[str], **options: object) -> RunResult:
        _assert_run_options(options, tmp_path)
        return _completed(command, 1, output)

    _install_runner(monkeypatch, run)

    assert refresh.main(["--check-only"]) == 1


def test_regenerate_replaces_all_references_after_exact_candidate_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _prepare_references(tmp_path, monkeypatch)
    calls: list[list[str]] = []

    def run(command: list[str], **options: object) -> RunResult:
        calls.append(command)
        _assert_run_options(options, tmp_path)
        if command == refresh._VISUAL_TEST_COMMAND:
            for test_name, relative_path in refresh._BASELINE_PATHS_BY_TEST.items():
                reference = tmp_path / relative_path
                assert not reference.exists()
                reference.write_bytes(f"new-{test_name}".encode())
            return _completed(command, 1, _candidate_failures(tmp_path))
        assert command == refresh._GIT_STATUS_COMMAND
        status = "".join(
            f" M {relative_path}\0" for relative_path in refresh._BASELINE_PATHS_BY_TEST.values()
        )
        return _completed(command, 0, status)

    _install_runner(monkeypatch, run)

    assert refresh.main(["--regenerate"]) == 0
    assert calls == [refresh._VISUAL_TEST_COMMAND, refresh._GIT_STATUS_COMMAND]
    for test_name, relative_path in refresh._BASELINE_PATHS_BY_TEST.items():
        assert (tmp_path / relative_path).read_bytes() == f"new-{test_name}".encode()


def test_regenerate_restores_references_when_candidate_generation_is_incomplete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    originals = _prepare_references(tmp_path, monkeypatch)

    def run(command: list[str], **options: object) -> RunResult:
        _assert_run_options(options, tmp_path)
        first_test, first_path = next(iter(refresh._BASELINE_PATHS_BY_TEST.items()))
        (tmp_path / first_path).write_bytes(b"partial-candidate")
        output = (
            f"FAILED tests/e2e/test_visual_regression.py::{first_test} - Failed: "
            f"Generated visual baseline candidate at {tmp_path / first_path}. "
            "Review and commit it before rerunning the test.\n"
        )
        return _completed(command, 1, output)

    _install_runner(monkeypatch, run)

    assert refresh.main(["--regenerate"]) == 1
    for test_name, relative_path in refresh._BASELINE_PATHS_BY_TEST.items():
        assert (tmp_path / relative_path).read_bytes() == originals[test_name]


def test_regenerate_restores_references_when_non_png_file_changed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    originals = _prepare_references(tmp_path, monkeypatch)

    def run(command: list[str], **options: object) -> RunResult:
        _assert_run_options(options, tmp_path)
        if command == refresh._VISUAL_TEST_COMMAND:
            for relative_path in refresh._BASELINE_PATHS_BY_TEST.values():
                (tmp_path / relative_path).write_bytes(b"new")
            return _completed(command, 1, _candidate_failures(tmp_path))
        return _completed(
            command,
            0,
            " M tests/e2e/__snapshots__/test_visual_regression/home.png\0?? unexpected.txt\0",
        )

    _install_runner(monkeypatch, run)

    assert refresh.main(["--regenerate"]) == 1
    assert "non-PNG paths changed: unexpected.txt" in capsys.readouterr().err
    for test_name, relative_path in refresh._BASELINE_PATHS_BY_TEST.items():
        assert (tmp_path / relative_path).read_bytes() == originals[test_name]


def test_regenerate_propagates_git_status_failure_and_restores_references(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    originals = _prepare_references(tmp_path, monkeypatch)

    def run(command: list[str], **options: object) -> RunResult:
        _assert_run_options(options, tmp_path)
        if command == refresh._VISUAL_TEST_COMMAND:
            for relative_path in refresh._BASELINE_PATHS_BY_TEST.values():
                (tmp_path / relative_path).write_bytes(b"new")
            return _completed(command, 1, _candidate_failures(tmp_path))
        return _completed(command, 128, "fatal: not a git repository\n")

    _install_runner(monkeypatch, run)

    assert refresh.main(["--regenerate"]) == 128
    for test_name, relative_path in refresh._BASELINE_PATHS_BY_TEST.items():
        assert (tmp_path / relative_path).read_bytes() == originals[test_name]
