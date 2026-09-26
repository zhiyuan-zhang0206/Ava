"""Contracts for the independent Redis data-plane credential rotation.

PostgreSQL has nothing to rotate here: the owner is NOLOGIN, the administrator
is the OS user over the owner-only socket, and application logins are write
generations that rotate with release transitions.
"""

from __future__ import annotations

import json
import stat
from dataclasses import asdict
from pathlib import Path
from typing import cast
from urllib.parse import urlsplit

import pytest

from scripts import rotate_data_plane_secrets as rotate
from shared import cluster
from shared.config import settings

_OLD_REDIS_ADMIN = "old-redis-admin"
_NEW_REDIS_ADMIN = "new-redis-admin"
_OLD_REDIS_RUNTIME = "old-redis-runtime"
_NEW_REDIS_RUNTIME = "new-redis-runtime"
_ENV = f"AVA_REDIS_ADMIN_PASSWORD={_OLD_REDIS_ADMIN}\nAVA_REDIS_PASSWORD={_OLD_REDIS_RUNTIME}\n"


def _state(scope: str = "both") -> rotate.RotationState:
    return rotate.RotationState(
        scope=scope,
        old_redis_admin_password=_OLD_REDIS_ADMIN,
        new_redis_admin_password=_NEW_REDIS_ADMIN,
        old_redis_password=_OLD_REDIS_RUNTIME,
        new_redis_password=_NEW_REDIS_RUNTIME,
        redis_port=16380,
        redis_host="127.0.0.1",
        redis_user="ava",
    )


def _patch_gateway_home(monkeypatch: pytest.MonkeyPatch, home: Path) -> None:
    monkeypatch.setattr(rotate, "ava_home", lambda: home)

    def _record(_home: Path) -> cluster.ClusterRecord:
        return cluster.ClusterRecord(
            ports=cast("cluster.ClusterPorts", {"postgres": 15433, "redis": 16380}),
            gateway_home=str(home),
            created_at="now",
        )

    monkeypatch.setattr(rotate, "get_record", _record)
    monkeypatch.setattr(settings, "profile", None)
    monkeypatch.setattr(settings.data_plane, "cluster_secret", "")
    monkeypatch.setattr(settings.data_plane, "db_url", "postgresql://ava@127.0.0.1:16433/ava")
    monkeypatch.setattr(
        settings.data_plane, "redis_url", "redis://ava:old-redis-runtime@127.0.0.1:16380/0"
    )


def test_build_state_mints_fresh_redis_values_even_without_a_bearer(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Redis always authenticates, so a no-secret single box rotates too."""
    _patch_gateway_home(monkeypatch, tmp_path)
    (tmp_path / ".env").write_text(_ENV)

    state = rotate.build_state()

    assert state.scope == "both"
    assert state.new_redis_admin_password not in {"", state.old_redis_admin_password}
    assert state.new_redis_password not in {"", state.old_redis_password}
    assert (state.redis_port, state.redis_user) == (16380, "ava")


def test_build_state_refuses_a_home_without_redis_credentials(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _patch_gateway_home(monkeypatch, tmp_path)
    (tmp_path / ".env").write_text(f"AVA_REDIS_ADMIN_PASSWORD={_OLD_REDIS_ADMIN}\n")
    with pytest.raises(RuntimeError, match="cutover_db_authority"):
        rotate.build_state()


def test_build_state_refuses_an_agent_process_profile(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _patch_gateway_home(monkeypatch, tmp_path)
    monkeypatch.setattr(settings, "profile", "agent")
    (tmp_path / ".env").write_text(_ENV)
    with pytest.raises(RuntimeError, match="gateway context"):
        rotate.build_state()


@pytest.mark.parametrize("resume", [False, True])
def test_main_reports_an_agent_context_guard_cleanly(
    resume: bool,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _patch_gateway_home(monkeypatch, tmp_path)
    monkeypatch.setattr(settings, "profile", "agent")
    args = ["--resume", str(tmp_path / "missing-state.json")] if resume else []

    assert rotate.main(args) == 1
    err = capsys.readouterr().err
    assert "gateway context" in err
    assert "unset AVA_PROCESS_PROFILE" in err
    assert "Traceback" not in err


def test_admin_scope_changes_the_redis_default_user_only(monkeypatch: pytest.MonkeyPatch) -> None:
    state = _state("admin")
    redis_commands: list[tuple[str, ...]] = []

    class _Redis:
        def __init__(self, **_kwargs: object) -> None:
            pass

        def __enter__(self) -> _Redis:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def execute_command(self, *args: str) -> None:
            redis_commands.append(args)

    def _working_redis_admin_password(_state: rotate.RotationState) -> str:
        return _OLD_REDIS_ADMIN

    monkeypatch.setattr(rotate, "_working_redis_admin_password", _working_redis_admin_password)
    monkeypatch.setattr(rotate.redis, "Redis", _Redis)

    rotate.apply_admin(state)
    rotate.apply_runner(state)  # admin scope: no ACL effect

    assert redis_commands == [("CONFIG", "SET", "requirepass", _NEW_REDIS_ADMIN)]
    assert state.redis_password == _OLD_REDIS_RUNTIME


def test_runner_scope_rotates_the_acl_user_of_the_redis_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _state("runner")
    acl_calls: list[tuple[str, dict[str, object]]] = []

    def _working_redis_admin_password(_state: rotate.RotationState) -> str:
        return _OLD_REDIS_ADMIN

    def _ensure_cluster_redis_acl(user: str, **kwargs: object) -> None:
        acl_calls.append((user, kwargs))

    monkeypatch.setattr(rotate, "_working_redis_admin_password", _working_redis_admin_password)
    monkeypatch.setattr(rotate, "ensure_cluster_redis_acl", _ensure_cluster_redis_acl)  # pyright: ignore[reportUnknownArgumentType]

    rotate.apply_admin(state)  # runner scope: no requirepass effect
    rotate.apply_runner(state)

    assert acl_calls == [
        (
            "ava",
            {
                "redis_admin_url": f"redis://default:{_OLD_REDIS_ADMIN}@127.0.0.1:16380",
                "runtime_password": _NEW_REDIS_RUNTIME,
                "channel_prefix": settings.data_plane.events_channel.removesuffix(":events"),
            },
        )
    ]


def test_write_env_syncs_the_redis_url_with_the_active_passwords(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _patch_gateway_home(monkeypatch, tmp_path)
    state = _state("runner")
    writes: list[dict[str, str]] = []

    def _upsert_env(_path: Path, values: dict[str, str], *, audit_site: str | None = None) -> None:
        writes.append(values)

    monkeypatch.setattr(rotate, "upsert_env", _upsert_env)

    rotate.write_env(state)

    assert writes == [
        {
            "AVA_REDIS_ADMIN_PASSWORD": _OLD_REDIS_ADMIN,
            "AVA_REDIS_PASSWORD": _NEW_REDIS_RUNTIME,
            "AVA_REDIS_URL": writes[0]["AVA_REDIS_URL"],
        }
    ]
    assert urlsplit(writes[0]["AVA_REDIS_URL"]).password == _NEW_REDIS_RUNTIME


def test_preflight_and_verify_probe_the_state_host_as_the_url_user(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Probes dial the URL-derived host (asserted against a foreign host) as the
    Redis `default` admin and as the redis_url ACL user, old then new values."""
    state = _state()
    state.redis_host = "10.0.0.7"
    probes: list[tuple[str, str, str]] = []

    def _redis_probe(host: str, _port: int, password: str, *, username: str) -> bool:
        probes.append((host, username, password))
        return True

    monkeypatch.setattr(rotate, "_redis_probe", _redis_probe)

    assert rotate.preflight(state) is True
    rotate.verify(state)
    assert probes == [
        ("10.0.0.7", "default", _OLD_REDIS_ADMIN),
        ("10.0.0.7", "ava", _OLD_REDIS_RUNTIME),
        ("10.0.0.7", "default", _NEW_REDIS_ADMIN),
        ("10.0.0.7", "ava", _NEW_REDIS_RUNTIME),
    ]


def test_dry_run_never_calls_a_mutating_phase(monkeypatch: pytest.MonkeyPatch) -> None:
    state = _state()
    mutations: list[str] = []

    def _build_state(_scope: str) -> rotate.RotationState:
        return state

    def _preflight(_state: rotate.RotationState) -> bool:
        return True

    def _apply_admin(_state: rotate.RotationState) -> None:
        mutations.append("admin")

    monkeypatch.setattr(settings, "profile", None)
    monkeypatch.setattr(rotate, "build_state", _build_state)
    monkeypatch.setattr(rotate, "preflight", _preflight)
    monkeypatch.setattr(rotate, "apply_admin", _apply_admin)

    assert rotate.main([]) == 0
    assert mutations == []


def test_recovery_state_is_owner_only(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(rotate, "ava_home", lambda: tmp_path)
    state = _state()
    path = state.save()

    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert rotate.RotationState.load(path) == state


def test_a_postgres_era_journal_is_refused_by_name(tmp_path: Path) -> None:
    data = {**asdict(_state()), "old_db_admin_password": "x", "pg_port": 5433}
    path = tmp_path / "data-plane-old.json"
    path.write_text(json.dumps(data))
    with pytest.raises(RuntimeError, match="not a Redis-only rotation journal"):
        rotate.RotationState.load(path)


def test_main_refuses_remote_managed_data_plane(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Rotation is a local-instance operation; a remote/SaaS plane rotates at
    the provider and the script must refuse to touch it (Task #1752)."""
    monkeypatch.setattr(settings.data_plane, "db_url", "postgresql://ava:pw@10.9.8.7:5432/ava")
    monkeypatch.setattr(settings.data_plane, "redis_url", "rediss://ava:pw@10.9.8.7:6380/0")
    assert rotate.main(["--scope", "admin"]) == 1
    err = capsys.readouterr().err
    assert "remote-managed" in err
    assert "rotates credentials at the provider" in err
