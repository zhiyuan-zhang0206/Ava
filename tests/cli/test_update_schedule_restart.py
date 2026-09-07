"""Update preserves persistent schedule terminals, including compatibility callers."""

from __future__ import annotations

from pathlib import Path

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
    monkeypatch.setattr(_update, "_do_stop", lambda *_a, **_kw: 0)  # pyright: ignore[reportUnknownArgumentType]


def test_pull_path_preserves_schedule_sessions(monkeypatch: pytest.MonkeyPatch, repo: Path) -> None:
    """Code updates preserve persistent schedule terminals through restart."""
    calls: list[tuple[list[str], Path]] = []

    monkeypatch.setattr(_local, "_restart_schedule_sessions", lambda: calls.append(([], repo)))
    _patch_leg(monkeypatch)

    assert (
        _local._run_gateway_local_update(
            repo, target_sha="abc123", pull_recover=("old123", set(), None), pull=True
        )
        == 0
    )
    assert calls == []


def test_noop_update_skips_schedule_bounce(monkeypatch: pytest.MonkeyPatch, repo: Path) -> None:
    """A no-op rollout (host already on the target commit) changes no code —
    sessions must not be bounced (an in-flight fire is not interrupted for
    nothing), even though the gateway still restarts."""
    calls: list[tuple[list[str], Path]] = []
    monkeypatch.setattr(_local, "_restart_schedule_sessions", lambda: calls.append(([], repo)))
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
    monkeypatch.setattr(_local, "_restart_schedule_sessions", lambda: calls.append(([], repo)))
    _patch_leg(monkeypatch)

    assert _local._run_gateway_local_update(repo, pull=False) == 0

    assert calls == []


def test_failed_boot_skips_schedule_bounce(monkeypatch: pytest.MonkeyPatch, repo: Path) -> None:
    """A failed `ava start` recovers to last-known-good — the sessions are running
    the recovered (current-again) code, so they must not be bounced."""
    calls: list[tuple[list[str], Path]] = []
    monkeypatch.setattr(_local, "_restart_schedule_sessions", lambda: calls.append(([], repo)))
    monkeypatch.setattr(_local, "_boot_gateway_fresh", lambda *_a, **_kw: 1)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(_local, "_checkout_and_sync", lambda *_a, **_kw: None)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(_update, "_do_stop", lambda *_a, **_kw: 0)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(_local, "_recover_rc", lambda *_a, **_kw: 1)  # pyright: ignore[reportUnknownArgumentType]

    assert (
        _local._run_gateway_local_update(
            repo, target_sha="abc123", pull_recover=("abc123", set(), None), pull=True
        )
        == 1
    )

    assert calls == []


def test_legacy_bounce_entry_keeps_existing_terminal(monkeypatch: pytest.MonkeyPatch) -> None:
    backend = _FakeBackend([session_name("schedule-1")])
    monkeypatch.setattr("shared.session_backend.get_shell_backend", lambda: backend)
    _local._restart_schedule_sessions()
    assert backend.killed == []
    assert backend.live == [session_name("schedule-1")]


def test_bounce_schedule_sessions_entry_calls_helper(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[bool] = []
    monkeypatch.setattr(_local, "_restart_schedule_sessions", lambda: calls.append(True))

    assert _local.main(["--bounce-schedule-sessions"]) == 0
    assert calls == [True]
