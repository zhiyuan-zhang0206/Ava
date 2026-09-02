"""Contract tests for host-wide, generation-owned PTY allocation freeze."""

from __future__ import annotations

from pathlib import Path

import pytest

from shared.config import settings
from shared.pty_sessions import allocation_freeze
from shared.pty_sessions import cli as pty_cli


@pytest.fixture(autouse=True)
def _isolated_host_registry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    registry = tmp_path / "host" / "clusters.json"
    monkeypatch.setattr(settings.general, "cluster_registry", registry)
    return registry


def test_freeze_is_host_wide_and_records_operator_generation(
    _isolated_host_registry: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    first_home = tmp_path / "cluster-a"
    second_home = tmp_path / "cluster-b"
    monkeypatch.setattr(settings.general, "ava_home", first_home)
    first_state_path = allocation_freeze.state_path()
    monkeypatch.setattr(settings.general, "ava_home", second_home)

    frozen = allocation_freeze.freeze(holder="operator-1818", reason="bounded cleanup")

    assert allocation_freeze.state_path() == first_state_path
    assert first_state_path.parent == _isolated_host_registry.parent
    assert frozen.status == "frozen"
    assert frozen.generation
    assert frozen.holder == "operator-1818"
    assert frozen.reason == "bounded cleanup"
    assert frozen.created_at is not None
    assert allocation_freeze.read() == frozen


def test_resume_is_generation_scoped_and_never_clears_a_newer_owner() -> None:
    first = allocation_freeze.freeze(holder="first", reason="first cleanup")
    assert first.generation is not None

    assert not allocation_freeze.resume("stale-generation")
    assert allocation_freeze.read() == first
    assert allocation_freeze.resume(first.generation)
    active = allocation_freeze.read()
    assert active.status == "inactive"
    assert active.generation == first.generation
    assert allocation_freeze.current_generation() == first.generation

    second = allocation_freeze.freeze(holder="second", reason="second cleanup")
    assert second.generation is not None and second.generation != first.generation
    assert not allocation_freeze.resume(first.generation)
    assert allocation_freeze.read() == second


def test_corrupt_marker_fails_closed_and_refusal_removes_envfile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    path = allocation_freeze.state_path()
    path.parent.mkdir(parents=True)
    path.write_text("{broken-json")
    spawned: list[str] = []

    def _reap_none(_name: str) -> int:
        return 0

    def _has_no_session(_name: str) -> bool:
        return False

    def _record_spawn(name: str, *_args: str) -> int:
        spawned.append(name)
        return 0

    monkeypatch.setattr(pty_cli, "_reap_orphaned_hosts", _reap_none)
    monkeypatch.setattr(pty_cli, "has_session", _has_no_session)
    monkeypatch.setattr(pty_cli, "_spawn_host", _record_spawn)
    envfile = tmp_path / "handoff.env"
    envfile.write_text("A=1\n")

    assert pty_cli._op_new("ava-agent-1-shell-0", [str(tmp_path), str(envfile)]) == 1
    assert allocation_freeze.read().status == "invalid"
    assert spawned == []
    assert not envfile.exists()
    assert "allocation refused" in capsys.readouterr().err


def test_frozen_new_reuses_live_name_but_refuses_missing_until_resume(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    live = {"ava-agent-1-shell-0-existing"}
    spawned: list[str] = []

    def _reap_none(_name: str) -> int:
        return 0

    def _has_session(name: str) -> bool:
        return name in live

    def _spawn(name: str, *_args: str) -> int:
        spawned.append(name)
        live.add(name)
        return 0

    monkeypatch.setattr(pty_cli, "_reap_orphaned_hosts", _reap_none)
    monkeypatch.setattr(pty_cli, "has_session", _has_session)
    monkeypatch.setattr(pty_cli, "_spawn_host", _spawn)
    frozen = allocation_freeze.freeze(holder="operator", reason="manifest and cleanup")
    assert frozen.generation is not None

    existing_env = tmp_path / "existing.env"
    existing_env.write_text("A=1\n")
    assert pty_cli._op_new("ava-agent-1-shell-0-existing", [str(tmp_path), str(existing_env)]) == 0
    assert not existing_env.exists()

    missing_env = tmp_path / "missing.env"
    missing_env.write_text("A=1\n")
    assert pty_cli._op_new("ava-agent-1-shell-1-missing", [str(tmp_path), str(missing_env)]) == 1
    assert not missing_env.exists()
    assert spawned == []
    assert frozen.generation in capsys.readouterr().err

    assert allocation_freeze.resume(frozen.generation)
    resumed_env = tmp_path / "resumed.env"
    resumed_env.write_text("A=1\n")
    assert pty_cli._op_new("ava-agent-1-shell-1-missing", [str(tmp_path), str(resumed_env)]) == 0
    assert spawned == ["ava-agent-1-shell-1-missing"]


def test_active_generation_rejects_a_live_name_from_a_prior_generation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A reconcile cannot idempotently reuse an exact session from the prior flip."""
    name = "ava-agent-1-shell-0-stale"
    spawned: list[str] = []

    def _reap_none(_name: str) -> int:
        return 0

    def _has_live(_name: str) -> bool:
        return True

    def _prior_generation(_name: str) -> str:
        return "prior-generation"

    def _record_spawn(
        session_name: str,
        _cwd: str,
        _envfile: str,
        _generation: str | None,
        _cmd_b64: str,
    ) -> int:
        spawned.append(session_name)
        return 0

    monkeypatch.setattr(pty_cli, "_reap_orphaned_hosts", _reap_none)
    monkeypatch.setattr(pty_cli, "has_session", _has_live)
    monkeypatch.setattr(pty_cli, "session_generation", _prior_generation)
    monkeypatch.setattr(pty_cli, "_spawn_host", _record_spawn)
    frozen = allocation_freeze.freeze(holder="operator", reason="generation flip")
    assert frozen.generation is not None
    assert allocation_freeze.resume(frozen.generation)
    envfile = tmp_path / "stale.env"
    envfile.write_text("A=1\n")

    assert pty_cli._op_new(name, [str(tmp_path), str(envfile)]) == 1

    assert spawned == []
    assert not envfile.exists()
    assert "belongs to a prior generation" in capsys.readouterr().err
