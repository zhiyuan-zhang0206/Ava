"""Session VIRTUAL_ENV projection follows the session working directory."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from ava.shell import sessions
from shared import bootstrap, dotenv_boot


class _CapturingBackend:
    """Minimal session backend that records the environment handed to a shell."""

    def __init__(self) -> None:
        self.environments: list[dict[str, str]] = []

    def new_session(
        self,
        _name: str,
        _command: str,
        _cwd: Path,
        *,
        env: dict[str, str],
    ) -> bool:
        self.environments.append(env)
        return True


def test_create_session_activates_only_checkout_cwds(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Worktree sessions retain PATH but cannot inherit another checkout's venv."""

    checkout = tmp_path / "checkout"
    inside = checkout / "nested"
    sibling_worktree = checkout / ".worktrees" / "feature"
    claude_sibling_worktree = checkout / ".claude" / "worktrees" / "feature"
    outside = tmp_path / "worktree"
    inside.mkdir(parents=True)
    sibling_worktree.mkdir(parents=True)
    claude_sibling_worktree.mkdir(parents=True)
    outside.mkdir()
    backend = _CapturingBackend()
    activations: list[bool] = []
    monkeypatch.setattr(sessions, "_next_session_index_from_db", lambda: 1)
    monkeypatch.setattr(sessions, "_shell_prefix", lambda: "session-")
    monkeypatch.setattr(sessions, "get_shell_backend", lambda: backend)
    monkeypatch.setattr(sessions, "repo_root", lambda: checkout)

    def forward(*, activate_venv: bool = True) -> dict[str, str]:
        activations.append(activate_venv)
        return {"PATH": "/venv/bin:/usr/bin", **({"VIRTUAL_ENV": "/venv"} if activate_venv else {})}

    monkeypatch.setattr(sessions, "forward_env_dict", forward)

    # TTL registration needs the gateway DB — not this test's concern.
    def _noop_record_ttl(_sid: int, _ttl: float) -> None:
        return None

    monkeypatch.setattr(sessions, "_record_ttl", _noop_record_ttl)
    sessions._create_session("inside", cwd=str(inside), ttl=120)
    sessions._create_session("sibling", cwd=str(sibling_worktree), ttl=120)
    sessions._create_session("claude-sibling", cwd=str(claude_sibling_worktree), ttl=120)
    sessions._create_session("outside", cwd=str(outside), ttl=120)

    assert activations == [True, False, False, False]
    assert backend.environments[0]["VIRTUAL_ENV"] == "/venv"
    assert "VIRTUAL_ENV" not in backend.environments[1]
    assert "VIRTUAL_ENV" not in backend.environments[2]
    assert "VIRTUAL_ENV" not in backend.environments[3]
    assert backend.environments[1]["PATH"] == "/venv/bin:/usr/bin"


def test_watcher_override_reaches_backend_without_changing_generic_sessions(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The backend gets runner URLs only from the watcher's explicit override."""
    from ava import watcher

    runner_db = "postgresql://ava_runner:runner@127.0.0.1:6433/ava"
    runner_redis = "redis://ava:runner@127.0.0.1:6380/0"
    monkeypatch.setenv("AVA_PROCESS_PROFILE", "agent")
    monkeypatch.delenv("AVA_LAUNCHER_PROFILE", raising=False)
    monkeypatch.setitem(os.environ, "AVA_DB_URL", runner_db)
    monkeypatch.setitem(os.environ, "AVA_REDIS_URL", runner_redis)
    monkeypatch.setitem(os.environ, "AVA_CLUSTER_SECRET", "test-secret")
    monkeypatch.setattr(dotenv_boot, "_HOME", Path.home() / ".ava")
    monkeypatch.setattr(dotenv_boot, "_ANCHORED", True)
    monkeypatch.setattr(watcher, "_agent_id", lambda: 42)
    monkeypatch.setattr(sessions, "_next_session_index_from_db", lambda: 1)
    monkeypatch.setattr(sessions, "_shell_prefix", lambda: "session-")

    def workspace_for_test(_agent_id: int) -> Path:
        return tmp_path

    def record_no_ttl(_sid: int, _ttl: float) -> None:
        return None

    monkeypatch.setattr(sessions, "workspace_dir", workspace_for_test)
    monkeypatch.setattr(sessions, "repo_root", lambda: tmp_path / "checkout")
    monkeypatch.setattr(sessions, "_record_ttl", record_no_ttl)
    backend = _CapturingBackend()

    class _CapturedSessionError(RuntimeError):
        pass

    original_new_session = backend.new_session

    def capture_then_stop(name: str, command: str, cwd: Path, *, env: dict[str, str]) -> bool:
        original_new_session(name, command, cwd, env=env)
        raise _CapturedSessionError

    monkeypatch.setattr(backend, "new_session", capture_then_stop)
    monkeypatch.setattr(sessions, "get_shell_backend", lambda: backend)

    with pytest.raises(_CapturedSessionError):
        watcher.launch("pass", "10s", name="env-probe")
    watcher_env = backend.environments[0]
    assert watcher_env["AVA_DB_URL"] == runner_db
    assert watcher_env["AVA_REDIS_URL"] == runner_redis

    with pytest.raises(_CapturedSessionError):
        sessions._create_session("ordinary", cwd=str(tmp_path), ttl=120)
    generic_env = backend.environments[1]
    assert "AVA_DB_URL" not in generic_env
    assert "AVA_REDIS_URL" not in generic_env


def test_watcher_fail_fast_precedes_session_allocation(monkeypatch: pytest.MonkeyPatch) -> None:
    from ava import watcher

    monkeypatch.setattr(dotenv_boot, "_HOME", Path.home() / ".ava")
    monkeypatch.setattr(dotenv_boot, "_ANCHORED", True)
    monkeypatch.setattr(bootstrap, "config_source_is_local", lambda: True)
    monkeypatch.delenv("AVA_PROCESS_PROFILE", raising=False)
    monkeypatch.delenv("AVA_LAUNCHER_PROFILE", raising=False)
    monkeypatch.setitem(os.environ, "AVA_CLUSTER_SECRET", "test-secret")
    monkeypatch.setitem(os.environ, "AVA_DB_URL", "postgresql://ava_main:owner@127.0.0.1:6433/ava")
    monkeypatch.setitem(os.environ, "AVA_REDIS_URL", "redis://ava:runner@127.0.0.1:6380/0")
    monkeypatch.setattr(watcher, "_agent_id", lambda: 42)

    def forbid_session(*_args: object, **_kwargs: object) -> tuple[int, str]:
        raise AssertionError("session allocation started")

    monkeypatch.setattr(sessions, "_create_session", forbid_session)
    with pytest.raises(RuntimeError, match="Launch the watcher from an agent-profile process"):
        watcher.launch("pass", "10s", name="refused")


# Watcher runner projection at the session boundary.

_RUNNER_DB = "postgresql://ava_runner:runner@127.0.0.1:6433/ava"
_OWNER_DB = "postgresql://ava_main:owner@127.0.0.1:6433/ava"
_RUNNER_REDIS = "redis://ava:runner@127.0.0.1:6380/0"


def _secured_default_home(monkeypatch: pytest.MonkeyPatch) -> None:
    # watcher_runner_env reads these launcher values live, before session birth.
    monkeypatch.setattr(dotenv_boot, "_HOME", Path.home() / ".ava")
    monkeypatch.setattr(dotenv_boot, "_ANCHORED", True)
    monkeypatch.setattr(bootstrap, "config_source_is_local", lambda: True)
    monkeypatch.setitem(os.environ, "AVA_CLUSTER_SECRET", "test-secret")


@pytest.mark.parametrize("profile_key", ["AVA_PROCESS_PROFILE", "AVA_LAUNCHER_PROFILE"])
def test_agent_context_forwards_only_runner_urls(
    monkeypatch: pytest.MonkeyPatch, profile_key: str
) -> None:
    _secured_default_home(monkeypatch)
    monkeypatch.delenv("AVA_PROCESS_PROFILE", raising=False)
    monkeypatch.delenv("AVA_LAUNCHER_PROFILE", raising=False)
    monkeypatch.setenv(profile_key, "agent")
    monkeypatch.setitem(os.environ, "AVA_DB_URL", _RUNNER_DB)
    monkeypatch.setitem(os.environ, "AVA_REDIS_URL", _RUNNER_REDIS)

    assert dotenv_boot.watcher_runner_env() == {
        "AVA_DB_URL": _RUNNER_DB,
        "AVA_REDIS_URL": _RUNNER_REDIS,
    }


@pytest.mark.parametrize(
    ("profile", "db_url", "redis_url"),
    [
        (None, _RUNNER_DB, _RUNNER_REDIS),
        ("agent", _OWNER_DB, _RUNNER_REDIS),
        ("agent", "postgresql://[::1", _RUNNER_REDIS),
        ("agent", _RUNNER_DB, "redis://[::1"),
        ("agent", _RUNNER_DB, "redis://default:owner@127.0.0.1:6380/0"),
    ],
)
def test_secured_default_home_refuses_missing_or_unsafe_projection(
    monkeypatch: pytest.MonkeyPatch, profile: str | None, db_url: str, redis_url: str
) -> None:
    _secured_default_home(monkeypatch)
    monkeypatch.delenv("AVA_LAUNCHER_PROFILE", raising=False)
    if profile is None:
        monkeypatch.delenv("AVA_PROCESS_PROFILE", raising=False)
    else:
        monkeypatch.setenv("AVA_PROCESS_PROFILE", profile)
    monkeypatch.setitem(os.environ, "AVA_DB_URL", db_url)
    monkeypatch.setitem(os.environ, "AVA_REDIS_URL", redis_url)

    with pytest.raises(RuntimeError, match="Launch the watcher from an agent-profile process"):
        dotenv_boot.watcher_runner_env()


@pytest.mark.parametrize(
    "redis_url",
    [
        "redis://127.0.0.1:6380/0",
        "redis://default:owner@127.0.0.1:6380/0",
        "redis://ava@127.0.0.1:6380/0",
    ],
)
def test_no_secret_home_never_forwards_an_unauthenticated_or_admin_redis_url(
    monkeypatch: pytest.MonkeyPatch, redis_url: str
) -> None:
    """Redis always authenticates, so an empty bearer does not relax the
    watcher projection: only the named runtime ACL URL with its password is
    forwarded, never the `default` admin user or a password-less URL."""
    _secured_default_home(monkeypatch)
    monkeypatch.setitem(os.environ, "AVA_CLUSTER_SECRET", "")
    monkeypatch.delenv("AVA_LAUNCHER_PROFILE", raising=False)
    monkeypatch.setenv("AVA_PROCESS_PROFILE", "agent")
    monkeypatch.setitem(os.environ, "AVA_DB_URL", _RUNNER_DB)
    monkeypatch.setitem(os.environ, "AVA_REDIS_URL", redis_url)

    assert dotenv_boot.watcher_runner_env() == {}
    monkeypatch.setitem(os.environ, "AVA_REDIS_URL", _RUNNER_REDIS)
    assert dotenv_boot.watcher_runner_env() == {
        "AVA_DB_URL": _RUNNER_DB,
        "AVA_REDIS_URL": _RUNNER_REDIS,
    }


@pytest.mark.parametrize("different_home", [False, True])
def test_other_home_or_pure_runner_keeps_existing_child_boot(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, different_home: bool
) -> None:
    _secured_default_home(monkeypatch)
    monkeypatch.delenv("AVA_PROCESS_PROFILE", raising=False)
    monkeypatch.delenv("AVA_LAUNCHER_PROFILE", raising=False)
    monkeypatch.setitem(os.environ, "AVA_DB_URL", _OWNER_DB)
    monkeypatch.setitem(os.environ, "AVA_REDIS_URL", _RUNNER_REDIS)
    if different_home:
        monkeypatch.setattr(dotenv_boot, "_HOME", tmp_path / "other-cluster")
    else:
        monkeypatch.setattr(bootstrap, "config_source_is_local", lambda: False)

    assert dotenv_boot.watcher_runner_env() == {}


def test_unprojected_agent_child_still_hits_owner_url_guard(
    tmp_path: Path,
) -> None:
    """A separate agent-profile interpreter still refuses an owner `.env` URL."""
    home = tmp_path / ".ava"
    home.mkdir()
    (home / ".env").write_text(
        "\n".join(
            (
                f"AVA_DB_URL={_OWNER_DB}",
                f"AVA_REDIS_URL={_RUNNER_REDIS}",
                "AVA_CLUSTER_SECRET=test-secret",
                "AVA_MACHINE_SERVE_GATEWAY=true",
            )
        )
        + "\n"
    )
    env = os.environ.copy()
    env.update(
        {
            "HOME": str(tmp_path),
            "AVA_HOME": str(home),
            "AVA_HOME_OVERRIDE": "1",
            "AVA_PROCESS_PROFILE": "agent",
        }
    )
    for key in ("AVA_DB_URL", "AVA_REDIS_URL", "AVA_CLUSTER_SECRET", "VIRTUAL_ENV"):
        env.pop(key, None)
    child = subprocess.run(
        [
            sys.executable,
            "-c",
            "from shared.config import settings\n"
            "try:\n"
            "    settings.data_plane.db_url\n"
            "except Exception as exc:\n"
            "    print(type(exc).__name__, 'agent-profile processes must receive a runner-class' in str(exc))\n",
        ],
        env=env,
        cwd=Path(__file__).resolve().parents[2],
        capture_output=True,
        text=True,
        check=False,
    )
    assert child.returncode == 0, child.stderr
    assert child.stdout.strip() == "ValidationError True"
