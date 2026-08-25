"""Rollout config-boundary locks for a fresh gateway start.

The rollout orchestrator survives the checkout and starts the new tree in a
child process.  That child may materialize new data-plane credentials in the
unit ``.env``; the surviving parent must adopt them before its next database
write (the cluster pin).
"""

from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import urlsplit

import pytest

from cli.commands import _update_local as _local
from cli.commands import update as _update
from shared.config import settings

_OLD_PASSWORD = "old-password"  # noqa: S105 — test fixture
_NEW_DB_PASSWORD = "new-db-password"  # noqa: S105 — test fixture
_NEW_REDIS_ADMIN_PASSWORD = "new-redis-admin-password"  # noqa: S105 — test fixture
_NEW_REDIS_RUNTIME_PASSWORD = "new-runtime-password"  # noqa: S105 — test fixture


def test_successful_fresh_start_adopts_credentials_written_by_child(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    old_db = f"postgresql://ava:{_OLD_PASSWORD}@127.0.0.1:16433/ava"
    old_redis = f"redis://ava:{_OLD_PASSWORD}@127.0.0.1:16380/0"
    new_db = f"postgresql://ava:{_NEW_DB_PASSWORD}@127.0.0.1:16433/ava"
    new_redis = f"redis://ava:{_NEW_REDIS_RUNTIME_PASSWORD}@127.0.0.1:16380/0"
    env_path = tmp_path / ".env"
    env_path.write_text(
        f"AVA_DB_URL={old_db}\n"
        f"AVA_REDIS_URL={old_redis}\n"
        f"AVA_DB_ADMIN_PASSWORD={_OLD_PASSWORD}\n"
        f"AVA_REDIS_ADMIN_PASSWORD={_OLD_PASSWORD}\n"
        f"AVA_REDIS_PASSWORD={_OLD_PASSWORD}\n"
    )

    monkeypatch.setattr(settings.general, "ava_home", tmp_path)
    monkeypatch.setattr(settings.data_plane, "db_url", old_db)
    monkeypatch.setattr(settings.data_plane, "redis_url", old_redis)
    monkeypatch.setattr(settings.data_plane, "db_admin_password", _OLD_PASSWORD)
    monkeypatch.setattr(settings.data_plane, "redis_admin_password", _OLD_PASSWORD)
    for key, value in {
        "AVA_DB_URL": old_db,
        "AVA_REDIS_URL": old_redis,
        "AVA_DB_ADMIN_PASSWORD": _OLD_PASSWORD,
        "AVA_REDIS_ADMIN_PASSWORD": _OLD_PASSWORD,
        "AVA_REDIS_PASSWORD": _OLD_PASSWORD,
    }.items():
        monkeypatch.setenv(key, value)

    def _stop(*_args: object, **_kwargs: object) -> None:
        return None

    monkeypatch.setattr(_update, "_do_stop", _stop)

    def _start_child(_repo: Path, _preserve: frozenset[str]) -> int:
        env_path.write_text(
            f"AVA_DB_URL={new_db}\n"
            f"AVA_REDIS_URL={new_redis}\n"
            f"AVA_DB_ADMIN_PASSWORD={_NEW_DB_PASSWORD}\n"
            f"AVA_REDIS_ADMIN_PASSWORD={_NEW_REDIS_ADMIN_PASSWORD}\n"
            f"AVA_REDIS_PASSWORD={_NEW_REDIS_RUNTIME_PASSWORD}\n"
        )
        return 0

    monkeypatch.setattr(_local, "_boot_gateway_fresh", _start_child)

    assert _local._run_gateway_local_update(tmp_path, pull=False) == 0

    assert urlsplit(settings.data_plane.db_url).password == _NEW_DB_PASSWORD
    assert urlsplit(settings.data_plane.redis_url).password == _NEW_REDIS_RUNTIME_PASSWORD
    assert settings.data_plane.db_admin_password == _NEW_DB_PASSWORD
    assert settings.data_plane.redis_admin_password == _NEW_REDIS_ADMIN_PASSWORD
    assert os.environ["AVA_DB_URL"] == new_db
    assert os.environ["AVA_REDIS_URL"] == new_redis
    assert os.environ["AVA_REDIS_PASSWORD"] == _NEW_REDIS_RUNTIME_PASSWORD


def test_gateway_child_keeps_restarter_down_until_orchestration_finishes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    commands: list[list[str]] = []

    class _Result:
        returncode = 0

    def _run(argv: list[str], **_kwargs: object) -> _Result:
        commands.append([str(arg) for arg in argv])
        return _Result()

    monkeypatch.setattr(_update.subprocess, "run", _run)

    assert _local._boot_gateway_fresh(tmp_path, frozenset()) == 0

    assert commands == [
        [
            str(tmp_path / ".venv" / "bin" / "ava"),
            "start",
            "--persist-services",
            "--no-readiness-gate",
            "--disable-service",
            "restarter",
        ]
    ]


def test_interrupted_child_adopts_credentials_before_recovery(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    order: list[str] = []

    def _snapshot(**_kwargs: object) -> tuple[str, set[str], Path | None]:
        return "old-sha", set(), None

    def _checkout(*_args: object, **_kwargs: object) -> None:
        return None

    def _refresh(_repo: Path) -> None:
        return None

    def _stop(*_args: object, **_kwargs: object) -> None:
        return None

    monkeypatch.setattr(_local, "_snapshot_known_good", _snapshot)
    monkeypatch.setattr(_local, "_checkout_and_sync", _checkout)
    monkeypatch.setattr(_local, "_refresh_builtin_skills", _refresh)
    monkeypatch.setattr(_update, "_do_stop", _stop)

    def _interrupt(_repo: Path, _preserve: frozenset[str]) -> int:
        order.append("child")
        raise KeyboardInterrupt

    def _adopt() -> None:
        order.append("adopt")

    def _recover(*_args: object, **_kwargs: object) -> int:
        order.append("recover")
        return 1

    monkeypatch.setattr(_local, "_boot_gateway_fresh", _interrupt)
    monkeypatch.setattr(_local, "_adopt_child_data_plane_credentials", _adopt)
    monkeypatch.setattr(_local, "_recover_rc", _recover)

    assert _local._run_gateway_local_update(tmp_path, target_sha="new-sha", pull=True) == 1
    assert order == ["child", "adopt", "recover"]
