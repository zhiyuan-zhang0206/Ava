"""The gateway rollout leg bounces schedule runner sessions on a code change.

The fresh gateway's ScheduleManager re-adopts live `ava-schedule-<id>` sessions at
boot (liveness is the session name), so a code-change rollout must kill the old
checkout's runners itself — otherwise old runner code AND old materialized script
text keep serving until the session dies (Task #1746: all 8 schedules still ran
08-25 code after two rollouts). The bounce happens only on the pull path after a
successful `ava start`; a restart-only bounce (same code) and a failed boot
(recovery) leave sessions alone. A kill failure is a warning, never a rollout
failure — the post-rollout checklist's leftover-schedule_runner check is the
backstop.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from cli.commands import _update_local as _local
from cli.commands import update as _update
from shared.cluster import session_name


class _FakeBackend:
    def __init__(self, live: list[str]) -> None:
        self.live = live
        self.killed: list[str] = []

    def list_sessions(self, prefix: str = "") -> list[str]:
        return [s for s in self.live if s.startswith(prefix)]

    def kill_session(self, name: str, **_kw: object) -> tuple[bool, str]:
        self.killed.append(name)
        self.live = [s for s in self.live if s != name]
        return True, "forced"


class _RecordingSubprocess:
    """Fake only the local module's fresh-bounce child, never global subprocess."""

    def __init__(self, calls: list[tuple[list[str], Path]]) -> None:
        self.calls = calls

    def run(self, argv: list[str], *, cwd: Path, check: bool) -> SimpleNamespace:
        assert check is False
        self.calls.append((argv, cwd))
        return SimpleNamespace(returncode=0)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    r = tmp_path / "repo"
    r.mkdir()
    return r


def _patch_leg(monkeypatch: pytest.MonkeyPatch) -> None:
    """Drive `_run_gateway_local_update` with every external step stubbed.

    The pre-update recovery tuple belongs to orchestration, so callers pass it
    directly when they exercise the pull path."""
    monkeypatch.setattr(_local, "_checkout_and_sync", lambda *_a, **_kw: None)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(_local, "_boot_gateway_fresh", lambda *_a, **_kw: 0)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(_update, "_do_stop", lambda *_a, **_kw: None)  # pyright: ignore[reportUnknownArgumentType]


def test_pull_path_bounces_schedule_sessions_in_a_fresh_interpreter(
    monkeypatch: pytest.MonkeyPatch, repo: Path
) -> None:
    """A code-change rollout launches the bounce from the checked-out tree."""
    calls: list[tuple[list[str], Path]] = []

    monkeypatch.setattr(_local, "subprocess", _RecordingSubprocess(calls))
    _patch_leg(monkeypatch)

    assert (
        _local._run_gateway_local_update(
            repo, target_sha="abc123", pull_recover=("old123", set(), None), pull=True
        )
        == 0
    )
    assert calls == [
        (
            [
                sys.executable,
                "-m",
                "cli.commands._update_local",
                "--bounce-schedule-sessions",
            ],
            repo,
        )
    ]


def test_noop_update_skips_schedule_bounce(monkeypatch: pytest.MonkeyPatch, repo: Path) -> None:
    """A no-op rollout (host already on the target commit) changes no code —
    sessions must not be bounced (an in-flight fire is not interrupted for
    nothing), even though the gateway still restarts."""
    calls: list[tuple[list[str], Path]] = []
    monkeypatch.setattr(_local, "subprocess", _RecordingSubprocess(calls))
    _patch_leg(monkeypatch)

    assert (
        _local._run_gateway_local_update(
            repo, target_sha="abc123", pull_recover=("abc123", set(), None), pull=True
        )
        == 0
    )

    assert calls == []


def test_restart_only_skips_schedule_bounce(monkeypatch: pytest.MonkeyPatch, repo: Path) -> None:
    """A restart-only bounce changes no code — the sessions are current, so they
    must not be killed (an in-flight fire is not interrupted for nothing)."""
    calls: list[tuple[list[str], Path]] = []
    monkeypatch.setattr(_local, "subprocess", _RecordingSubprocess(calls))
    _patch_leg(monkeypatch)

    assert _local._run_gateway_local_update(repo, pull=False) == 0

    assert calls == []


def test_failed_boot_skips_schedule_bounce(monkeypatch: pytest.MonkeyPatch, repo: Path) -> None:
    """A failed `ava start` recovers to last-known-good — the sessions are running
    the recovered (current-again) code, so they must not be bounced."""
    calls: list[tuple[list[str], Path]] = []
    monkeypatch.setattr(_local, "subprocess", _RecordingSubprocess(calls))
    monkeypatch.setattr(_local, "_boot_gateway_fresh", lambda *_a, **_kw: 1)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(_local, "_checkout_and_sync", lambda *_a, **_kw: None)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(_update, "_do_stop", lambda *_a, **_kw: None)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(_local, "_recover_rc", lambda *_a, **_kw: 1)  # pyright: ignore[reportUnknownArgumentType]

    assert (
        _local._run_gateway_local_update(
            repo, target_sha="abc123", pull_recover=("abc123", set(), None), pull=True
        )
        == 1
    )

    assert calls == []


def test_kill_failure_is_non_fatal(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """A session that refuses to die must not fail the rollout — the old runner
    keeps serving and the post-rollout leftover check is the backstop."""

    class _StubbornBackend(_FakeBackend):
        def kill_session(self, name: str, **_kw: object) -> tuple[bool, str]:
            return False, "forced"

    backend = _StubbornBackend([session_name("schedule-1")])
    monkeypatch.setattr("shared.session_backend.get_shell_backend", lambda: backend)
    _local._restart_schedule_sessions()

    assert "kill failed (non-fatal)" in capsys.readouterr().err  # pyright: ignore[reportUnknownMemberType]


def test_no_sessions_is_a_noop(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    backend = _FakeBackend([])
    monkeypatch.setattr("shared.session_backend.get_shell_backend", lambda: backend)
    _local._restart_schedule_sessions()

    assert backend.killed == []
    assert "no schedule runner sessions" in capsys.readouterr().out  # pyright: ignore[reportUnknownMemberType]


def test_bounce_subprocess_failure_is_non_fatal(
    monkeypatch: pytest.MonkeyPatch, repo: Path, capsys: pytest.CaptureFixture
) -> None:
    """The post-boot backstop remains a warning when the fresh child cannot start."""

    class _FailingSubprocess:
        def run(self, *_args: object, **_kwargs: object) -> object:
            raise OSError("python vanished")

    monkeypatch.setattr(_local, "subprocess", _FailingSubprocess())
    _patch_leg(monkeypatch)

    assert (
        _local._run_gateway_local_update(
            repo, target_sha="abc123", pull_recover=("old123", set(), None), pull=True
        )
        == 0
    )
    assert "schedule session bounce failed (non-fatal)" in capsys.readouterr().err  # pyright: ignore[reportUnknownMemberType]


def test_bounce_subprocess_nonzero_is_non_fatal(
    monkeypatch: pytest.MonkeyPatch, repo: Path, capsys: pytest.CaptureFixture
) -> None:
    """A failed fresh bounce leaves the gateway rollout successful."""

    class _NonzeroSubprocess:
        def run(self, *_args: object, **_kwargs: object) -> SimpleNamespace:
            return SimpleNamespace(returncode=7)

    monkeypatch.setattr(_local, "subprocess", _NonzeroSubprocess())
    _patch_leg(monkeypatch)

    assert (
        _local._run_gateway_local_update(
            repo, target_sha="abc123", pull_recover=("old123", set(), None), pull=True
        )
        == 0
    )
    assert "schedule session bounce failed (non-fatal): rc=7" in capsys.readouterr().err  # pyright: ignore[reportUnknownMemberType]


def test_bounce_schedule_sessions_entry_calls_helper(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[bool] = []
    monkeypatch.setattr(_local, "_restart_schedule_sessions", lambda: calls.append(True))

    assert _local.main(["--bounce-schedule-sessions"]) == 0
    assert calls == [True]


def test_scan_failure_is_non_fatal(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """A backend that cannot even be scanned must not raise out of the helper."""

    class _BoomBackend:
        def list_sessions(self, prefix: str = "") -> list[str]:
            raise RuntimeError("pty daemon gone")

    boom = _BoomBackend()
    monkeypatch.setattr("shared.session_backend.get_shell_backend", lambda: boom)
    _local._restart_schedule_sessions()  # must not raise

    assert "non-fatal" in capsys.readouterr().err  # pyright: ignore[reportUnknownMemberType]
