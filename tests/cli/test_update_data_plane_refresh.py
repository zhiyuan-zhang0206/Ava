"""Rollout config-boundary locks for a fresh gateway start.

The rollout orchestrator survives the checkout and starts the new tree in a
child process.  That child may materialize new data-plane credentials in the
unit ``.env``; the surviving parent must adopt them before its next database
write (the cluster pin).
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import textwrap
import time
from pathlib import Path
from urllib.parse import urlsplit

import pytest

from cli.commands import _update_local as _local
from cli.commands import update as _update
from shared import dotenv_boot
from shared.config import settings
from shared.rollout_handoff import (
    ROLLOUT_PARENT_CREDENTIAL_HANDOFF_ENV,
    ROLLOUT_PARENT_CREDENTIAL_HANDOFF_VERSION,
)

_OLD_PASSWORD = "old-password"  # noqa: S105 — test fixture
_NEW_DB_PASSWORD = "new-db-password"  # noqa: S105 — test fixture
_NEW_REDIS_ADMIN_PASSWORD = "new-redis-admin-password"  # noqa: S105 — test fixture
_NEW_REDIS_RUNTIME_PASSWORD = "new-runtime-password"  # noqa: S105 — test fixture


def _set_parent_credentials(
    monkeypatch: pytest.MonkeyPatch, *, home: Path, db_url: str, redis_url: str
) -> None:
    monkeypatch.setattr(dotenv_boot, "AVA_ENV_PATH", home / ".env")
    monkeypatch.setattr(dotenv_boot, "AVA_MIRROR_ENV_PATH", home / "absent-mirror.env")
    monkeypatch.setattr(settings.general, "ava_home", home)
    # refresh_data_plane_settings() replaces the whole sub-model. Record that
    # object boundary so teardown restores the session's provisioned DB model,
    # not only fields on the now-detached old object.
    monkeypatch.setattr(settings, "data_plane", settings.data_plane)
    monkeypatch.setattr(settings.data_plane, "db_url", db_url)
    monkeypatch.setattr(settings.data_plane, "redis_url", redis_url)
    monkeypatch.setattr(settings.data_plane, "db_admin_password", _OLD_PASSWORD)
    monkeypatch.setattr(settings.data_plane, "redis_admin_password", _OLD_PASSWORD)
    for key, value in {
        "AVA_HOME": str(home),
        "AVA_DB_URL": db_url,
        "AVA_REDIS_URL": redis_url,
        "AVA_DB_ADMIN_PASSWORD": _OLD_PASSWORD,
        "AVA_REDIS_ADMIN_PASSWORD": _OLD_PASSWORD,
        "AVA_REDIS_PASSWORD": _OLD_PASSWORD,
    }.items():
        monkeypatch.setitem(os.environ, key, value)


def _pid_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def _wait_for_pid_exit(pid: int, *, timeout_s: float) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if not _pid_exists(pid):
            return True
        time.sleep(0.01)
    return not _pid_exists(pid)


def _terminate_test_child(pid: int) -> None:
    if not _pid_exists(pid):
        return
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    if _wait_for_pid_exit(pid, timeout_s=1.0):
        return
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        return


@pytest.mark.parametrize(
    ("handoff", "expected"),
    (
        (None, False),
        ("1", False),
        ("v2", False),
        (ROLLOUT_PARENT_CREDENTIAL_HANDOFF_VERSION, True),
    ),
)
def test_rollout_parent_handoff_is_versioned_and_consumed_once(
    monkeypatch: pytest.MonkeyPatch, handoff: str | None, expected: bool
) -> None:
    from shared.rollout_handoff import consume_parent_credential_handoff

    if handoff is None:
        monkeypatch.delenv(ROLLOUT_PARENT_CREDENTIAL_HANDOFF_ENV, raising=False)
    else:
        monkeypatch.setenv(ROLLOUT_PARENT_CREDENTIAL_HANDOFF_ENV, handoff)

    assert consume_parent_credential_handoff() is expected
    assert ROLLOUT_PARENT_CREDENTIAL_HANDOFF_ENV not in os.environ


def test_real_start_module_consumes_the_parent_handoff(tmp_path: Path) -> None:
    result_path = tmp_path / "consumed"
    source = (
        "import os\n"
        "from pathlib import Path\n"
        "from cli.commands.start import _consume_rollout_parent_handoff\n"
        "from shared.rollout_handoff import ROLLOUT_PARENT_CREDENTIAL_HANDOFF_ENV\n"
        "assert _consume_rollout_parent_handoff()\n"
        "assert ROLLOUT_PARENT_CREDENTIAL_HANDOFF_ENV not in os.environ\n"
        f"Path({str(result_path)!r}).write_text('consumed')\n"
    )
    env = dict(os.environ)
    env[ROLLOUT_PARENT_CREDENTIAL_HANDOFF_ENV] = ROLLOUT_PARENT_CREDENTIAL_HANDOFF_VERSION
    completed = subprocess.run(  # noqa: S603 — fixed interpreter + in-test source
        [sys.executable, "-c", source],
        cwd=Path(__file__).resolve().parents[2],
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr
    assert result_path.read_text() == "consumed"


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

    _set_parent_credentials(monkeypatch, home=tmp_path, db_url=old_db, redis_url=old_redis)

    def _stop(*_args: object, **_kwargs: object) -> int:
        return 0

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


def test_gateway_child_leaves_readiness_to_orchestration(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    commands: list[tuple[list[str], dict[str, str]]] = []

    class _Result:
        returncode = 0

    def _run(argv: list[str], **kwargs: object) -> _Result:
        commands.append(([str(arg) for arg in argv], dict(kwargs["env"])))  # type: ignore[arg-type]
        return _Result()

    monkeypatch.setattr(_update.subprocess, "run", _run)

    assert _local._boot_gateway_fresh(tmp_path, frozenset()) == 0

    assert [argv for argv, _env in commands] == [
        [
            str(tmp_path / ".venv" / "bin" / "ava"),
            "start",
            "--persist-services",
            "--no-readiness-gate",
        ]
    ]
    assert (
        commands[0][1][ROLLOUT_PARENT_CREDENTIAL_HANDOFF_ENV]
        == ROLLOUT_PARENT_CREDENTIAL_HANDOFF_VERSION
    )


def test_interrupted_child_adopts_credentials_before_recovery(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    order: list[str] = []

    def _checkout(*_args: object, **_kwargs: object) -> None:
        return None

    def _refresh(_repo: Path) -> None:
        return None

    def _stop(*_args: object, **_kwargs: object) -> int:
        return 0

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

    assert (
        _local._run_gateway_local_update(
            tmp_path, target_sha="new-sha", pull_recover=("old-sha", set(), None), pull=True
        )
        == 1
    )
    assert order == ["child", "adopt", "recover"]


def test_adoption_failure_attempts_recovery(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A replay failure is a rollout failure, not an escape around rollback."""
    order: list[str] = []

    monkeypatch.setattr(_local, "_checkout_and_sync", lambda *_args, **_kwargs: None)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(_local, "_refresh_builtin_skills", lambda _repo: None)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(_update, "_do_stop", lambda *_args, **_kwargs: 0)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(_local, "_boot_gateway_fresh", lambda *_args: order.append("child") or 0)  # pyright: ignore[reportUnknownArgumentType]

    def _fail_adoption() -> None:
        order.append("adopt")
        raise RuntimeError("journal replay failed")

    monkeypatch.setattr(_local, "_adopt_child_data_plane_credentials", _fail_adoption)
    monkeypatch.setattr(
        _local,
        "_recover_rc",
        lambda *_args, **_kwargs: order.append("recover") or 1,  # pyright: ignore[reportUnknownArgumentType]
    )

    assert (
        _local._run_gateway_local_update(
            tmp_path, target_sha="new-sha", pull_recover=("old-sha", set(), None), pull=True
        )
        == 1
    )
    assert order == ["child", "adopt", "recover"]


def test_postgres_password_mutation_cannot_block_adoption_failure_recovery(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Inject the split failure at its dangerous boundary: Postgres already has
    the new owner password, while the surviving parent still has the old one.
    Recovery must roll back through local admin, reset, sync, and restart."""
    from cli.commands import _update_recover as _recover

    order: list[str] = []
    actual_pg_password = _OLD_PASSWORD

    monkeypatch.setattr(
        _local,
        "_checkout_and_sync",
        lambda *_args, **_kwargs: None,  # pyright: ignore[reportUnknownArgumentType]
    )
    monkeypatch.setattr(_local, "_refresh_builtin_skills", lambda _repo: None)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(_update, "_do_stop", lambda *_args, **_kwargs: 0)  # pyright: ignore[reportUnknownArgumentType]

    def _boot(*_args: object) -> int:
        nonlocal actual_pg_password
        actual_pg_password = _NEW_DB_PASSWORD
        order.append("child:postgres-password-mutated")
        return 0

    def _fail_adoption() -> None:
        assert actual_pg_password == _NEW_DB_PASSWORD
        order.append("adopt:redis-rewrite-failed")
        raise RuntimeError("redis CONFIG REWRITE failed after Postgres mutation")

    def _rollback(_target: set[str], *, local_admin: bool = False) -> list[str]:
        assert actual_pg_password != _OLD_PASSWORD
        assert local_admin is True
        order.append("rollback:local-admin")
        return []

    def _reset(sha: str) -> None:
        order.append(f"reset:{sha}")

    class _Result:
        returncode = 0

    class _RecoverySubprocess:
        @staticmethod
        def run(argv: list[str], **_kwargs: object) -> _Result:
            if len(argv) >= 2 and argv[0].endswith("ava") and argv[1] == "start":
                order.append("ava-start")
            return _Result()

    def _sync(_repo: object, *, timeout_s: float = 600.0) -> _Result:
        order.append("uv-sync")
        return _Result()

    monkeypatch.setattr(_local, "_boot_gateway_fresh", _boot)
    monkeypatch.setattr(_local, "_adopt_child_data_plane_credentials", _fail_adoption)
    monkeypatch.setattr(_recover, "rollback_schema_to", _rollback)
    monkeypatch.setattr(_recover, "git_reset_hard", _reset)
    monkeypatch.setattr(_recover, "run_uv_sync", _sync)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(_recover, "subprocess", _RecoverySubprocess)

    assert (
        _local._run_gateway_local_update(
            tmp_path, target_sha="new-sha", pull_recover=("old-sha", set(), None), pull=True
        )
        == 1
    )
    assert order == [
        "child:postgres-password-mutated",
        "adopt:redis-rewrite-failed",
        "rollback:local-admin",
        "reset:old-sha",
        "uv-sync",
        "ava-start",
    ]


def test_real_sigint_kills_child_and_replays_journal_before_recovery(tmp_path: Path) -> None:
    """Exercise the real subprocess/SIGINT boundary after the child journals secrets."""
    repo_root = Path(__file__).resolve().parents[2]
    home = tmp_path / "home"
    home.mkdir()
    ready = tmp_path / "journal-ready"
    child_pid = tmp_path / "child-pid"
    events = tmp_path / "events"
    old_db = f"postgresql://ava:{_OLD_PASSWORD}@127.0.0.1:16433/ava"
    old_redis = f"redis://ava:{_OLD_PASSWORD}@127.0.0.1:16380/0"
    new_db = f"postgresql://ava:{_NEW_DB_PASSWORD}@127.0.0.1:16433/ava"
    new_redis = f"redis://ava:{_NEW_REDIS_RUNTIME_PASSWORD}@127.0.0.1:16380/0"
    (home / ".env").write_text(
        f"AVA_CLUSTER_SECRET={_OLD_PASSWORD}\nAVA_DB_URL={old_db}\n"
        f"AVA_REDIS_URL={old_redis}\nAVA_DB_ADMIN_PASSWORD={_OLD_PASSWORD}\n"
        f"AVA_REDIS_ADMIN_PASSWORD={_OLD_PASSWORD}\n"
        f"AVA_REDIS_PASSWORD={_OLD_PASSWORD}\nAVA_PGBOUNCER_ENABLED=false\n"
    )

    ava_bin = tmp_path / ".venv" / "bin" / "ava"
    ava_bin.parent.mkdir(parents=True)
    ava_bin.write_text(
        textwrap.dedent(
            f"""\
            #!{sys.executable}
            import os
            import sys
            import time
            from pathlib import Path
            sys.path.insert(0, {str(repo_root)!r})
            from cli.commands import _data_plane_admin_secrets as split
            from cli.commands.start import _consume_rollout_parent_handoff
            from shared.platform import file_lock
            assert _consume_rollout_parent_handoff()
            with file_lock(split._transition_lock_path(), timeout_s=10.0):
                split._write_transition(split._Transition(
                    db_admin_password={_NEW_DB_PASSWORD!r},
                    redis_admin_password={_NEW_REDIS_ADMIN_PASSWORD!r},
                    redis_password={_NEW_REDIS_RUNTIME_PASSWORD!r},
                    db_url={new_db!r}, redis_url={new_redis!r},
                ))
                Path({str(child_pid)!r}).write_text(str(os.getpid()))
                Path({str(ready)!r}).write_text("ready")
                while True:
                    time.sleep(0.01)
            """
        )
    )
    ava_bin.chmod(0o755)

    parent_source = textwrap.dedent(
        f"""\
        from pathlib import Path
        from dotenv import dotenv_values
        from cli.commands import _data_plane_admin_secrets as split
        from cli.commands import _update_local as local
        from cli.commands import update
        from shared import cluster
        from shared.config import settings
        home = Path({str(home)!r})
        events = Path({str(events)!r})
        settings.general.ava_home = home
        settings.data_plane.cluster_secret = {_OLD_PASSWORD!r}
        settings.data_plane.db_admin_password = {_OLD_PASSWORD!r}
        settings.data_plane.redis_admin_password = {_OLD_PASSWORD!r}
        settings.data_plane.db_url = {old_db!r}
        settings.data_plane.redis_url = {old_redis!r}
        settings.data_plane.pgbouncer_enabled = False
        split.ava_home = lambda: home
        split.get_record = lambda _home: cluster.ClusterRecord(
            ports={{"postgres": 15433, "redis": 16380, "pgbouncer": 16433}},
            gateway_home=str(home), created_at="now",
        )
        split.ensure_cluster_role = lambda *_args, **_kwargs: None
        split._working_redis_admin_password = lambda *_args: {_NEW_REDIS_ADMIN_PASSWORD!r}
        split.ensure_cluster_redis_acl = lambda *_args, **_kwargs: None
        class Redis:
            def __init__(self, **_kwargs): pass
            def __enter__(self): return self
            def __exit__(self, *_args): return None
            def execute_command(self, *_args): return None
        import redis
        redis.Redis = Redis
        local._checkout_and_sync = lambda *_args, **_kwargs: None
        local._refresh_builtin_skills = lambda _repo: None
        update._do_stop = lambda *_args, **_kwargs: 0
        def recover(*_args, **_kwargs):
            values = dotenv_values(home / ".env")
            assert values["AVA_DB_ADMIN_PASSWORD"] == {_NEW_DB_PASSWORD!r}
            assert settings.data_plane.db_admin_password == {_NEW_DB_PASSWORD!r}
            assert not split._transition_path().exists()
            events.write_text("adopted-before-recovery")
            return 1
        local._recover_rc = recover
        rc = local._run_gateway_local_update(
            Path({str(tmp_path)!r}),
            target_sha="new-sha",
            pull_recover=("old-sha", set(), None),
            pull=True,
        )
        assert rc == 1
        """
    )
    env = dict(os.environ)
    env.update(
        {
            "AVA_HOME": str(home),
            "AVA_CLUSTER_SECRET": _OLD_PASSWORD,
            "AVA_DB_URL": old_db,
            "AVA_REDIS_URL": old_redis,
            "AVA_DB_ADMIN_PASSWORD": _OLD_PASSWORD,
            "AVA_REDIS_ADMIN_PASSWORD": _OLD_PASSWORD,
            "AVA_REDIS_PASSWORD": _OLD_PASSWORD,
            "AVA_PGBOUNCER_ENABLED": "false",
            "AVA_CONFIG_FETCH": "skip",
        }
    )
    parent = subprocess.Popen(  # noqa: S603 — fixed interpreter + in-test source
        [sys.executable, "-c", parent_source],
        cwd=repo_root,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.monotonic() + 10.0
        while not ready.exists() and parent.poll() is None and time.monotonic() < deadline:
            time.sleep(0.01)
        assert ready.exists(), "fresh child did not journal its credential transition"
        parent.send_signal(signal.SIGINT)
        stdout, stderr = parent.communicate(timeout=15)
        assert parent.returncode == 0, f"stdout:\n{stdout}\nstderr:\n{stderr}"
        assert events.read_text() == "adopted-before-recovery"
        assert _wait_for_pid_exit(int(child_pid.read_text()), timeout_s=2.0), (
            "interrupted rollout child remained alive"
        )
    finally:
        if parent.poll() is None:
            parent.terminate()
            try:
                parent.wait(timeout=2)
            except subprocess.TimeoutExpired:
                parent.kill()
                parent.wait(timeout=2)
        if child_pid.exists():
            _terminate_test_child(int(child_pid.read_text()))


# ─── PITR restart must bounce PostgreSQL too ───────────────────────────────────


def test_pitr_restart_origin_classification() -> None:
    """Only the activation/rollback seam's restart origins bounce the data
    plane; every other origin keeps pg/redis up (migrations need them)."""
    assert _local._pitr_restart("pitr-activation:op-1:orc-1") is True
    assert _local._pitr_restart("pitr-rollback:op-1:orc-1") is True
    assert _local._pitr_restart("agent:1818") is False
    assert _local._pitr_restart("cli:macmini") is False
    assert _local._pitr_restart("") is False


def test_pitr_restart_stops_the_data_plane_but_plain_restart_keeps_it(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The 2026-08-30 activation stalled at wal_restart_pending because the
    cluster restart kept pg up and the ALTER SYSTEM'd WAL config never took
    effect. A PITR-seam restart must pass keep_infra=False to the stop leg."""
    captured: list[bool] = []

    def fake_do_stop(_repo: Path, *, keep_infra: bool, **_kwargs: object) -> int:
        captured.append(keep_infra)
        return 0

    def fake_boot(_repo: Path, _preserve_frontend: frozenset[str]) -> int:
        return 0

    monkeypatch.setattr(_update, "_do_stop", fake_do_stop)
    monkeypatch.setattr(_local, "_boot_gateway_fresh", fake_boot)
    monkeypatch.setattr(_local, "_adopt_child_data_plane_credentials", lambda: None)

    assert (
        _local._run_gateway_local_update(tmp_path, pull=False, origin="pitr-activation:op-1:orc-1")
        == 0
    )
    assert captured == [False]

    captured.clear()
    assert _local._run_gateway_local_update(tmp_path, pull=False, origin="cli:macmini") == 0
    assert captured == [True]
