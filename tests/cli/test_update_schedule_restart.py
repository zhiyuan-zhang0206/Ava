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

from pathlib import Path

import pytest

import cli.commands as _cli
from cli.commands import _update_local as _local
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


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    r = tmp_path / "repo"
    r.mkdir()
    return r


def _patch_leg(monkeypatch: pytest.MonkeyPatch, *, from_sha: str = "old123") -> None:
    """Drive `_run_gateway_local_update` with every external step stubbed.

    `from_sha` is the pre-update HEAD the known-good snapshot records — the
    rollout's no-op test passes the same value as the target."""
    monkeypatch.setattr(_local, "_snapshot_known_good", lambda **_kw: (from_sha, set(), None))  # pyright: ignore[reportUnknownArgumentType, reportUnknownLambdaType]
    monkeypatch.setattr(_local, "_checkout_and_sync", lambda *_a, **_kw: None)  # pyright: ignore[reportUnknownArgumentType, reportUnknownLambdaType]
    monkeypatch.setattr(_local, "_boot_gateway_fresh", lambda *_a, **_kw: 0)  # pyright: ignore[reportUnknownArgumentType, reportUnknownLambdaType]
    monkeypatch.setattr(_cli, "_do_stop", lambda *_a, **_kw: None)  # pyright: ignore[reportUnknownArgumentType, reportUnknownLambdaType]


def test_pull_path_bounces_schedule_sessions(
    monkeypatch: pytest.MonkeyPatch, repo: Path, capsys: pytest.CaptureFixture
) -> None:
    """A code-change rollout (host on old123, target abc123) kills the old
    checkout's live schedule sessions so the fresh gateway's reconcile loop
    relaunches them on the new code."""
    backend = _FakeBackend([session_name("schedule-1"), session_name("schedule-2")])
    monkeypatch.setattr("shared.session_backend.get_shell_backend", lambda: backend)  # pyright: ignore[reportUnknownArgumentType]
    _patch_leg(monkeypatch, from_sha="old123")

    assert _local._run_gateway_local_update(repo, target_sha="abc123", pull=True) == 0

    assert backend.killed == [session_name("schedule-1"), session_name("schedule-2")]
    assert "restart schedule runner session" in capsys.readouterr().out  # pyright: ignore[reportUnknownMemberType]


def test_noop_update_skips_schedule_bounce(
    monkeypatch: pytest.MonkeyPatch, repo: Path, capsys: pytest.CaptureFixture
) -> None:
    """A no-op rollout (host already on the target commit) changes no code —
    sessions must not be bounced (an in-flight fire is not interrupted for
    nothing), even though the gateway still restarts."""
    backend = _FakeBackend([session_name("schedule-1")])
    monkeypatch.setattr("shared.session_backend.get_shell_backend", lambda: backend)  # pyright: ignore[reportUnknownArgumentType]
    _patch_leg(monkeypatch, from_sha="abc123")

    assert _local._run_gateway_local_update(repo, target_sha="abc123", pull=True) == 0

    assert backend.killed == []


def test_restart_only_skips_schedule_bounce(
    monkeypatch: pytest.MonkeyPatch, repo: Path, capsys: pytest.CaptureFixture
) -> None:
    """A restart-only bounce changes no code — the sessions are current, so they
    must not be killed (an in-flight fire is not interrupted for nothing)."""
    backend = _FakeBackend([session_name("schedule-1")])
    monkeypatch.setattr("shared.session_backend.get_shell_backend", lambda: backend)  # pyright: ignore[reportUnknownArgumentType]
    _patch_leg(monkeypatch)

    assert _local._run_gateway_local_update(repo, pull=False) == 0

    assert backend.killed == []


def test_failed_boot_skips_schedule_bounce(monkeypatch: pytest.MonkeyPatch, repo: Path) -> None:
    """A failed `ava start` recovers to last-known-good — the sessions are running
    the recovered (current-again) code, so they must not be bounced."""
    backend = _FakeBackend([session_name("schedule-1")])
    monkeypatch.setattr("shared.session_backend.get_shell_backend", lambda: backend)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(_local, "_boot_gateway_fresh", lambda *_a, **_kw: 1)  # pyright: ignore[reportUnknownArgumentType, reportUnknownLambdaType]
    monkeypatch.setattr(_local, "_snapshot_known_good", lambda **_kw: ("abc123", set(), None))  # pyright: ignore[reportUnknownArgumentType, reportUnknownLambdaType]
    monkeypatch.setattr(_local, "_checkout_and_sync", lambda *_a, **_kw: None)  # pyright: ignore[reportUnknownArgumentType, reportUnknownLambdaType]
    monkeypatch.setattr(_cli, "_do_stop", lambda *_a, **_kw: None)  # pyright: ignore[reportUnknownArgumentType, reportUnknownLambdaType]
    monkeypatch.setattr(_local, "_recover_rc", lambda *_a, **_kw: 1)  # pyright: ignore[reportUnknownArgumentType, reportUnknownLambdaType]

    assert _local._run_gateway_local_update(repo, target_sha="abc123", pull=True) == 1

    assert backend.killed == []


def test_kill_failure_is_non_fatal(
    monkeypatch: pytest.MonkeyPatch, repo: Path, capsys: pytest.CaptureFixture
) -> None:
    """A session that refuses to die must not fail the rollout — the old runner
    keeps serving and the post-rollout leftover check is the backstop."""

    class _StubbornBackend(_FakeBackend):
        def kill_session(self, name: str, **_kw: object) -> tuple[bool, str]:
            return False, "forced"

    backend = _StubbornBackend([session_name("schedule-1")])
    monkeypatch.setattr("shared.session_backend.get_shell_backend", lambda: backend)  # pyright: ignore[reportUnknownArgumentType]
    _patch_leg(monkeypatch)

    assert _local._run_gateway_local_update(repo, target_sha="abc123", pull=True) == 0

    assert "kill failed (non-fatal)" in capsys.readouterr().err  # pyright: ignore[reportUnknownMemberType]


def test_no_sessions_is_a_noop(
    monkeypatch: pytest.MonkeyPatch, repo: Path, capsys: pytest.CaptureFixture
) -> None:
    backend = _FakeBackend([])
    monkeypatch.setattr("shared.session_backend.get_shell_backend", lambda: backend)  # pyright: ignore[reportUnknownArgumentType]
    _patch_leg(monkeypatch)

    assert _local._run_gateway_local_update(repo, target_sha="abc123", pull=True) == 0

    assert backend.killed == []
    assert "no schedule runner sessions" in capsys.readouterr().out  # pyright: ignore[reportUnknownMemberType]


def test_scan_failure_is_non_fatal(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """A backend that cannot even be scanned must not raise out of the helper."""

    class _BoomBackend:
        def list_sessions(self, prefix: str = "") -> list[str]:
            raise RuntimeError("pty daemon gone")

    boom = _BoomBackend()
    monkeypatch.setattr("shared.session_backend.get_shell_backend", lambda: boom)  # pyright: ignore[reportUnknownArgumentType, reportUnknownLambdaType]
    _local._restart_schedule_sessions()  # must not raise

    assert "non-fatal" in capsys.readouterr().err  # pyright: ignore[reportUnknownMemberType]
