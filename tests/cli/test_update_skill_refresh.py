"""The rollout legs refresh repo-native skill copies after the pull (issue #1289).

Converge is bootstrap-only for repo-native skills (the R5 ruling): it lands
missing copies and never updates one. The product rollout is the one moment the
tree advances, so both legs run `ava skill update` once per rollout in a fresh
subprocess (this interpreter's imports are pre-pull code) — and never on a
restart-only bounce, where no code changed. Conflicts (locally edited copies)
and unexpected failures are warnings, never a rollout failure: skills are
derived state.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

import cli.commands as _cli
from cli.commands import _update_agent_runner as _runner
from cli.commands import _update_local as _local
from shared import host_deploy_state
from shared.deploy_timing import UV_SYNC_TIMEOUT_S


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    r = tmp_path / "repo"
    r.mkdir()
    return r


def test_gateway_leg_refreshes_on_pull_path(monkeypatch: pytest.MonkeyPatch, repo: Path) -> None:
    refresh_calls: list[Path] = []
    monkeypatch.setattr(_local, "_checkout_and_sync", lambda *_a, **_kw: None)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(_local, "_boot_gateway_fresh", lambda *_a, **_kw: 0)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(_local, "_refresh_builtin_skills", refresh_calls.append)
    monkeypatch.setattr(_cli, "_do_stop", lambda *_a, **_kw: 0)  # pyright: ignore[reportUnknownArgumentType]

    assert (
        _local._run_gateway_local_update(
            repo, target_sha="abc123", pull_recover=("old123", set(), None), pull=True
        )
        == 0
    )
    assert refresh_calls == [repo]


def test_gateway_leg_skips_refresh_on_restart_only(
    monkeypatch: pytest.MonkeyPatch, repo: Path
) -> None:
    refresh_calls: list[Path] = []
    monkeypatch.setattr(_local, "_boot_gateway_fresh", lambda *_a, **_kw: 0)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(_local, "_refresh_builtin_skills", refresh_calls.append)
    monkeypatch.setattr(_cli, "_do_stop", lambda *_a, **_kw: 0)  # pyright: ignore[reportUnknownArgumentType]

    assert _local._run_gateway_local_update(repo, pull=False) == 0
    assert refresh_calls == []


def test_gateway_refresh_helper_never_raises(
    monkeypatch: pytest.MonkeyPatch, repo: Path, capsys: pytest.CaptureFixture
) -> None:
    """rc=1 (conflicts), a non-zero rc, and OSError are warnings; the helper
    must never raise."""
    from cli.commands import update as _up

    class _R1:
        returncode = 1

    monkeypatch.setattr(_up, "subprocess", type("F", (), {"run": lambda *_a, **_kw: _R1()})())
    _local._refresh_builtin_skills(repo)
    assert "conflicts reported" in capsys.readouterr().err  # pyright: ignore[reportUnknownMemberType]

    class _R2:
        returncode = 7

    monkeypatch.setattr(_up, "subprocess", type("F", (), {"run": lambda *_a, **_kw: _R2()})())
    _local._refresh_builtin_skills(repo)
    assert "rc=7" in capsys.readouterr().err  # pyright: ignore[reportUnknownMemberType]

    def _boom(*_a: object, **_kw: object) -> object:
        raise OSError("launcher gone")

    monkeypatch.setattr(_up, "subprocess", type("F", (), {"run": staticmethod(_boom)})())
    _local._refresh_builtin_skills(repo)
    assert "non-fatal" in capsys.readouterr().err  # pyright: ignore[reportUnknownMemberType]


def test_runner_leg_refreshes_after_checkout(monkeypatch: pytest.MonkeyPatch, repo: Path) -> None:
    refresh_calls: list[tuple[Path, Path]] = []

    class _FakeBackend:
        def venv_launcher(self, _name: str, root: Path) -> Path:
            launcher = root / "ava"
            launcher.touch()
            return launcher

    def _record(_repo: Path, _ava_bin: Path) -> None:
        refresh_calls.append((_repo, _ava_bin))

    monkeypatch.setattr(host_deploy_state, "try_acquire_updater_lock", lambda: False)
    monkeypatch.setattr(_runner, "validate_migrations_at_ref", lambda *_a, **_kw: None)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(_runner, "platform_backend", _FakeBackend)
    monkeypatch.setattr(_runner, "_refresh_builtin_skills", _record)
    monkeypatch.setattr(_cli, "_preflight_probes", lambda: 0)
    monkeypatch.setattr(_cli, "_do_stop", lambda *_a, **_kw: 0)  # pyright: ignore[reportUnknownArgumentType]

    class _FakeSubprocess:
        def run(self, *_a, **_kw):
            return type("R", (), {"returncode": 0})()

    def sync_verified(
        _repo: Path,
        *,
        timeout_s: float = UV_SYNC_TIMEOUT_S,
    ) -> subprocess.CompletedProcess[bytes]:
        return subprocess.CompletedProcess(["uv", "sync"], returncode=0)

    monkeypatch.setattr(_runner, "subprocess", _FakeSubprocess())
    monkeypatch.setattr(_runner, "run_uv_sync_verified", sync_verified)

    assert (
        _runner._run_agent_runner_self_update_inner(
            repo,
            target_sha="abc123",
            from_sha="old123",
            post_checkout=True,
            restart_only=False,
            mode="none",
        )
        == 0
    )
    assert len(refresh_calls) == 1
    assert refresh_calls[0][0] == repo
    assert refresh_calls[0][1] == repo / "ava"


def test_runner_leg_skips_refresh_on_restart_only(
    monkeypatch: pytest.MonkeyPatch, repo: Path
) -> None:
    refresh_calls: list[tuple[Path, Path]] = []

    class _FakeBackend:
        def venv_launcher(self, _name: str, root: Path) -> Path:
            launcher = root / "ava"
            launcher.touch()
            return launcher

    def _record(_repo: Path, _ava_bin: Path) -> None:
        refresh_calls.append((_repo, _ava_bin))

    monkeypatch.setattr(_runner, "platform_backend", _FakeBackend)
    monkeypatch.setattr(_runner, "_refresh_builtin_skills", _record)
    monkeypatch.setattr(_cli, "_preflight_probes", lambda: 0)
    monkeypatch.setattr(_cli, "_do_stop", lambda *_a, **_kw: 0)  # pyright: ignore[reportUnknownArgumentType]

    class _FakeSubprocess:
        def run(self, *_a, **_kw):
            return type("R", (), {"returncode": 0})()

    monkeypatch.setattr(_runner, "subprocess", _FakeSubprocess())

    assert _runner._run_agent_runner_self_update_inner(repo, restart_only=True, mode="none") == 0
    assert refresh_calls == []


def test_runner_refresh_helper_never_raises(
    monkeypatch: pytest.MonkeyPatch, repo: Path, capsys: pytest.CaptureFixture
) -> None:
    """rc=1 (conflicts) and OSError are warnings; the helper must never raise."""

    class _R1:
        returncode = 1

    monkeypatch.setattr(_runner, "subprocess", type("F", (), {"run": lambda *_a, **_kw: _R1()})())
    _runner._refresh_builtin_skills(repo, repo / "ava")
    assert "conflicts reported" in capsys.readouterr().err  # pyright: ignore[reportUnknownMemberType]

    def _boom(*_a: object, **_kw: object) -> object:
        raise OSError("launcher gone")

    monkeypatch.setattr(_runner, "subprocess", type("F", (), {"run": staticmethod(_boom)})())
    _runner._refresh_builtin_skills(repo, repo / "ava")
    assert "non-fatal" in capsys.readouterr().err  # pyright: ignore[reportUnknownMemberType]
