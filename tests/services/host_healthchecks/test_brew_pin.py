"""Read-only Homebrew pin observations cannot confuse command failure with health."""

import subprocess
from pathlib import Path
from unittest.mock import Mock

import pytest

from services.ava_root_glue.diagnostic_probes import brew_pins


def test_missing_approved_pin_is_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    query = Mock(
        side_effect=[
            subprocess.CompletedProcess([], 0, stdout="postgresql@17\n"),
            subprocess.CompletedProcess([], 0, stdout="postgresql@17\nredis\n"),
        ]
    )
    monkeypatch.setattr("shared.proc.run_bounded", query)
    assert brew_pins().verdict.value == "down"
    assert query.call_args_list[0].args[0] == ["brew", "list", "--pinned"]
    assert all(call.kwargs["timeout"] == 5 for call in query.call_args_list)


def test_command_failure_is_not_an_empty_pin_set(monkeypatch: pytest.MonkeyPatch) -> None:
    query = Mock(side_effect=subprocess.CalledProcessError(1, "brew"))
    monkeypatch.setattr("shared.proc.run_bounded", query)
    with pytest.raises(subprocess.CalledProcessError):
        brew_pins()


def test_pin_observation_uses_real_bounded_process_contract(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executable = tmp_path / "brew"
    executable.write_text("#!/bin/sh\nprintf 'postgresql@17\\n'\n")
    executable.chmod(0o700)
    monkeypatch.setenv("PATH", str(tmp_path))
    assert brew_pins().alive


def test_nonzero_brew_query_is_not_readiness(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executable = tmp_path / "brew"
    executable.write_text("#!/bin/sh\nexit 7\n")
    executable.chmod(0o700)
    monkeypatch.setenv("PATH", str(tmp_path))
    with pytest.raises(subprocess.CalledProcessError):
        brew_pins()
