"""`ava boot` — the retry loop the schedulers that cannot retry a boot job run.

Every test stubs `subprocess.run` and `time.sleep`; nothing here starts a
cluster or waits a real minute.
"""

from __future__ import annotations

import subprocess
import sys
import types
from pathlib import Path

import pytest

from cli import boot_retry
from shared.boot_policy import BOOT_RETRY_INTERVAL_S


@pytest.fixture
def runs() -> list[list[str]]:
    """Collects every child command `_stub_returncodes` sees."""
    return []


def _stub_returncodes(
    monkeypatch: pytest.MonkeyPatch, seen: list[list[str]], codes: list[int]
) -> list[float]:
    """Make the Nth `ava start` exit with codes[N] (last code repeats).
    Returns the list every sleep duration is appended to."""
    slept: list[float] = []
    monkeypatch.setattr(boot_retry.time, "sleep", slept.append)

    def fake_run(cmd: list[str], **_kw: object) -> types.SimpleNamespace:
        rc = codes[min(len(seen), len(codes) - 1)]
        seen.append(cmd)
        return types.SimpleNamespace(returncode=rc)

    monkeypatch.setattr(boot_retry.subprocess, "run", fake_run)
    return slept


def test_a_start_that_works_runs_once_and_never_sleeps(
    monkeypatch: pytest.MonkeyPatch, runs: list[list[str]]
) -> None:
    slept = _stub_returncodes(monkeypatch, runs, [0])
    assert boot_retry.run_boot([]) == 0
    assert len(runs) == 1
    assert slept == []


def test_a_transient_failure_is_retried_at_the_shared_interval(
    monkeypatch: pytest.MonkeyPatch, runs: list[list[str]]
) -> None:
    """The incident's shape: the first start races the VPN interface and exits
    1, the next one — a minute later — finds the gateway reachable."""
    slept = _stub_returncodes(monkeypatch, runs, [1, 0])
    assert boot_retry.run_boot([]) == 0
    assert len(runs) == 2
    assert slept == [BOOT_RETRY_INTERVAL_S]


def test_it_does_not_give_up_after_a_long_outage(
    monkeypatch: pytest.MonkeyPatch, runs: list[list[str]]
) -> None:
    """No attempt cap — the loop is still trying three hours in.

    This is the property the OS watchdog probe does NOT provide: it revives a
    dead watchdog session and nothing else ("It does NOT run `ava start`",
    per `cli/commands/_cluster_watchdog_probe.py`), and neither it nor the
    watchdog it revives runs the gateway env refresh, converge, or the pg/redis
    bring-up. So a `ava boot` that gave up would leave the host down until a
    human noticed — the original outage, with a longer fuse. macOS gets the same
    guarantee from launchd's KeepAlive, which also has no cap.
    """
    slept = _stub_returncodes(monkeypatch, runs, [1] * 180 + [0])
    assert boot_retry.run_boot([]) == 0
    assert len(runs) == 181
    assert slept == [BOOT_RETRY_INTERVAL_S] * 180


def test_the_child_is_this_interpreter_running_ava_start(
    monkeypatch: pytest.MonkeyPatch, runs: list[list[str]]
) -> None:
    """`sys.executable -m cli.main`, not the `ava` console script: the Windows
    task names the venv interpreter outright so PATH cannot resolve a different
    `ava.exe`, and re-deriving it here would reopen exactly that."""
    _stub_returncodes(monkeypatch, runs, [0])
    boot_retry.run_boot([])
    assert runs[0] == [sys.executable, "-m", "cli.main", "start", "--no-readiness-gate"]


def test_start_flags_are_forwarded(monkeypatch: pytest.MonkeyPatch, runs: list[list[str]]) -> None:
    _stub_returncodes(monkeypatch, runs, [0])
    boot_retry.run_boot(["--machine-name", "laptop-host"])
    assert runs[0][-4:] == [
        "start",
        "--machine-name",
        "laptop-host",
        # Appended after the caller's flags, never in place of them: the boot path must
        # not be able to produce a readiness exit code, because this loop retries any
        # non-zero forever. tests/cli/test_start_readiness_gate.py owns the why.
        "--no-readiness-gate",
    ]


def _stub_run_capturing_stdio(
    monkeypatch: pytest.MonkeyPatch, stdio_seen: list[tuple[object, object]]
) -> None:
    """Stub `subprocess.run` recording each call's stdout/stderr wiring."""

    def fake_run(cmd: list[str], **kw: object) -> types.SimpleNamespace:
        stdio_seen.append((kw.get("stdout"), kw.get("stderr")))
        return types.SimpleNamespace(returncode=0)

    monkeypatch.setattr(boot_retry.subprocess, "run", fake_run)


def test_the_child_never_inherits_the_parents_stdio_handles(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The Windows boot task runs this loop under pythonw, whose stdio handles
    are invalid — a child that inherited them died at its FIRST print with
    OSError 22 before starting anything, so every retry failed identically and
    the loop never exited. The child's output must be routed explicitly:
    to `$AVA_HOME/logs/boot.log`, truncated per attempt (bounded size — a
    wedged host retrying for days keeps only the attempt a diagnostician
    needs), and closed once the attempt returns."""
    monkeypatch.setattr(boot_retry, "resolve_ava_home", lambda: (tmp_path, True))
    stdio_seen: list[tuple[object, object]] = []
    _stub_run_capturing_stdio(monkeypatch, stdio_seen)

    assert boot_retry.run_boot([]) == 0

    assert len(stdio_seen) == 1
    stdout, stderr = stdio_seen[0]
    assert stdout is not None and stderr is not None  # inheriting is the bug
    assert stderr is stdout  # one interleaved stream, like a console would be
    assert getattr(stdout, "name", None) == str(tmp_path / "logs" / "boot.log")
    assert getattr(stdout, "mode", None) == "wb"  # "wb" = truncate per attempt
    assert getattr(stdout, "closed", False)  # closed once the attempt returned


def test_an_unwritable_home_degrades_to_devnull_not_a_crash(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """This loop is the recovery path of last resort, so it must survive a home
    whose logs dir cannot even be created — the child still gets valid stdio."""
    not_a_dir = tmp_path / "occupied"
    not_a_dir.write_text("a file where the home should be")
    monkeypatch.setattr(boot_retry, "resolve_ava_home", lambda: (not_a_dir, True))
    stdio_seen: list[tuple[object, object]] = []
    _stub_run_capturing_stdio(monkeypatch, stdio_seen)

    assert boot_retry.run_boot([]) == 0

    assert stdio_seen == [(subprocess.DEVNULL, subprocess.DEVNULL)]


def test_cli_main_dispatches_boot_before_the_settings_gated_import(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`ava boot` must reach the loop even on a host whose Settings would fail —
    a start that failed for a config reason is still a start worth retrying."""
    from cli import main as cli_main

    forwarded: list[list[str]] = []
    monkeypatch.setattr("cli.boot_retry.run_boot", lambda argv: forwarded.append(argv) or 0)  # pyright: ignore[reportUnknownArgumentType]
    assert cli_main.main(["boot", "--machine-name", "x"]) == 0
    assert forwarded == [["--machine-name", "x"]]
