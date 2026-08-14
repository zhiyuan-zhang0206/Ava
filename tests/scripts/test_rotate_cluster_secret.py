"""Tests for scripts/rotate_cluster_secret.py.

The mutating phases are exercised against REAL, ephemeral Postgres + Redis
instances rather than mocks, so a bug that only shows up against genuine
password/ACL enforcement is caught. `tests/_containers.py`'s shared
`throwaway_postgres` initdb's with `-A trust` (deliberately, for the rest of
the suite) — every password would authenticate vacuously there — so this file
inits its own throwaway Postgres with `scram-sha-256` on TCP loopback, exactly
like `cli.commands._cluster_instance._pg_hba_body`. Redis is started with
`--requirepass` from the start, mirroring `_cluster_instance._start_redis`.

Not covered here (already covered elsewhere, out of this script's scope):
`ensure_cluster_role` / `ensure_cluster_redis_acl` / `ensure_pgbouncer`
correctness themselves — `tests/cli/test_pgbouncer_*.py` and the cluster
bring-up tests own that. This file treats them as given primitives and tests
the ROTATION on top: mint -> apply -> verify -> write_env -> resume.

Patch targets follow `conventions/python-conventions.md`'s "reach a
stubbable name through its owning module": every name under test
(`ava_home`, `AVA_ENV_PATH`, `apply_*`, `build_state`) is imported into
`rotate_cluster_secret` via a MODULE-level `from x import y`, so a stub must
target `rotate.<name>`, not the defining module.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from typing import cast

import psycopg
import pytest
import redis as redis_lib

from scripts import rotate_cluster_secret as rotate
from shared.cluster import ensure_cluster_role
from shared.config import settings
from shared.pg_tools import pg_tool
from tests._containers import _free_port, _wait_port

_IDENTITY = "ava_rotate_test"
_OLD_SECRET = "old-secret-AAAAAAAAAAAAAAAAAAAA"  # noqa: S105 — throwaway fixture secret, not real
_NEW_SECRET = "new-secret-BBBBBBBBBBBBBBBBBBBB"  # noqa: S105 — throwaway fixture secret, not real

# Mirrors DataPlaneSettings._validate_cluster_secret's allowed charset.
_ALLOWED_SECRET_CHARS = set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789._~-")


@contextmanager
def _scram_postgres() -> Generator[tuple[str, int]]:
    """A throwaway Postgres instance with `scram-sha-256` enforced on TCP
    loopback (the local unix socket stays `trust`, for passwordless admin
    provisioning — mirrors `cli.commands._cluster_instance._pg_hba_body`).
    Yields (admin_url_over_trust_socket, port)."""
    tmp = Path(tempfile.mkdtemp(prefix="ava-rotate-pg-"))
    data = tmp / "data"
    port = _free_port()
    subprocess.run(  # noqa: S603 — argv is the resolved initdb path + static flags
        [
            pg_tool("initdb"),
            "-D",
            str(data),
            "-U",
            "postgres",
            "-A",
            "trust",
            "--no-sync",
            "--encoding=UTF8",
            "--locale=C",
        ],
        check=True,
        capture_output=True,
    )
    (data / "pg_hba.conf").write_text(
        "local all all trust\n"
        "host all all 127.0.0.1/32 scram-sha-256\n"
        "host all all ::1/128 scram-sha-256\n"
    )
    subprocess.run(  # noqa: S603 — argv is the resolved pg_ctl path + static flags
        [
            pg_tool("pg_ctl"),
            "-D",
            str(data),
            "-l",
            str(tmp / "pg.log"),
            "-w",
            "-t",
            "60",
            "start",
            "-o",
            f"-p {port} -c listen_addresses=127.0.0.1 -c unix_socket_directories={tmp}",
        ],
        check=True,
        capture_output=True,
    )
    try:
        yield f"postgresql://postgres@/postgres?host={tmp}&port={port}", port
    finally:
        subprocess.run(  # noqa: S603 — argv is the resolved pg_ctl path + static flags
            [pg_tool("pg_ctl"), "-D", str(data), "-m", "immediate", "stop"],
            check=False,
            capture_output=True,
        )
        shutil.rmtree(tmp, ignore_errors=True)


@contextmanager
def _requirepass_redis(password: str) -> Generator[int]:
    """A throwaway redis-server with `--requirepass` set from the start (like
    prod's `_start_redis`, unlike `tests/_containers.py`'s passwordless one).
    Yields its port."""
    tmp = Path(tempfile.mkdtemp(prefix="ava-rotate-redis-"))
    port = _free_port()
    proc = subprocess.Popen(  # noqa: S603 — argv is the static redis-server path + flags
        [
            "redis-server",
            "--port",
            str(port),
            "--bind",
            "127.0.0.1",
            "--save",
            "",
            "--dir",
            str(tmp),
            "--requirepass",
            password,
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        _wait_port(port)
        yield port
    finally:
        proc.terminate()
        proc.wait(timeout=10)
        shutil.rmtree(tmp, ignore_errors=True)


@pytest.fixture
def rotation_fixture(
    monkeypatch: pytest.MonkeyPatch,
) -> Generator[rotate.RotationState]:
    """A full scratch cluster (role+db on scram pg, ACL user on requirepass
    redis, both provisioned with `_OLD_SECRET`) wired into `settings.data_plane`
    so the module under test reads it exactly like it would read a real
    cluster's `.env`-derived Settings. pgbouncer is left disabled — its own
    reload path has dedicated coverage in tests/cli/test_pgbouncer_*.py."""
    with (
        _scram_postgres() as (pg_admin_conn, pg_port),
        _requirepass_redis(_OLD_SECRET) as redis_port,
    ):
        ensure_cluster_role(_IDENTITY, base_admin_url=pg_admin_conn, cluster_secret=_OLD_SECRET)
        with psycopg.connect(pg_admin_conn, autocommit=True) as conn:
            conn.execute(f'CREATE DATABASE "{_IDENTITY}" OWNER "{_IDENTITY}"')  # type: ignore[arg-type]
        rotate.ensure_cluster_redis_acl(
            _IDENTITY,
            redis_admin_url=f"redis://default:{_OLD_SECRET}@127.0.0.1:{redis_port}",
            cluster_secret=_OLD_SECRET,
            channel_prefix="ava",
        )

        # The real `pg_admin_url()` resolves this cluster's OWN socket dir via
        # `ava_home()` / a `/tmp/ava-pg-*` glob (`_live_pg_socket_dir`) — this
        # throwaway instance lives at an unrelated tmp path, so the admin dial
        # is swapped for the one this fixture actually started. Socket-path
        # discovery has its own coverage; this file tests rotation on top of it.
        monkeypatch.setattr(rotate, "pg_admin_url", lambda _pg_port: pg_admin_conn)  # pyright: ignore[reportUnknownArgumentType]

        monkeypatch.setattr(
            settings.data_plane,
            "db_url",
            f"postgresql://{_IDENTITY}:{_OLD_SECRET}@127.0.0.1:{pg_port}/{_IDENTITY}",
        )
        monkeypatch.setattr(
            settings.data_plane,
            "redis_url",
            f"redis://{_IDENTITY}:{_OLD_SECRET}@127.0.0.1:{redis_port}/0",
        )
        monkeypatch.setattr(settings.data_plane, "cluster_secret", _OLD_SECRET)
        monkeypatch.setattr(settings.data_plane, "pgbouncer_enabled", False)

        # build_state derives the direct pg port and the pooler port from the
        # registry record (the one-URL design: AVA_DB_URL carries the pooler port
        # when pooling is on, so the URL is not a port source for the admin plane).
        # The scratch cluster has no real registry — hand it a record that names
        # the scratch pg port and no pooler (pgbouncer disabled -> port 0).
        from shared import cluster as _cl
        from shared import paths as _paths

        monkeypatch.setattr(_paths, "ava_home", lambda: Path("/x/.ava-rotate-test"))
        monkeypatch.setattr(
            _cl,
            "get_record",
            lambda _home: _cl.ClusterRecord(  # pyright: ignore[reportUnknownArgumentType]
                ports=cast("_cl.ClusterPorts", {"postgres": pg_port, "pgbouncer": 0}),
                gateway_home="/x/.ava-rotate-test",
                created_at="t",
            ),
        )

        yield rotate.build_state(new_secret=_NEW_SECRET)


def test_mint_secret_is_url_safe() -> None:
    secret = rotate.mint_secret()
    assert len(secret) >= 32
    assert set(secret) <= _ALLOWED_SECRET_CHARS


def test_build_state_rejects_identity_mismatch(monkeypatch: pytest.MonkeyPatch) -> None:
    from shared import cluster as _cl
    from shared import paths as _paths

    monkeypatch.setattr(settings.data_plane, "db_url", "postgresql://ava:s@127.0.0.1:5433/ava")
    monkeypatch.setattr(settings.data_plane, "redis_url", "redis://ava_other:s@127.0.0.1:6380/0")
    monkeypatch.setattr(_paths, "ava_home", lambda: Path("/x/.ava-t"))
    monkeypatch.setattr(
        _cl,
        "get_record",
        lambda _home: _cl.ClusterRecord(  # pyright: ignore[reportUnknownArgumentType]
            ports=cast("_cl.ClusterPorts", {"postgres": 5433, "pgbouncer": 0}),
            gateway_home="/x/.ava-t",
            created_at="t",
        ),
    )
    with pytest.raises(RuntimeError, match="identity"):
        rotate.build_state()


def test_build_state_refuses_without_registry_record(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The admin plane's ports are registry facts — a missing record is refused,
    never guessed from the URL (which carries the pooler port when pooling is
    on)."""
    from shared import cluster as _cl
    from shared import paths as _paths

    monkeypatch.setattr(settings.data_plane, "db_url", "postgresql://ava:s@127.0.0.1:6433/ava")
    monkeypatch.setattr(settings.data_plane, "redis_url", "redis://ava:s@127.0.0.1:6380/0")
    monkeypatch.setattr(_paths, "ava_home", lambda: Path("/x/.ava-t"))
    monkeypatch.setattr(_cl, "get_record", lambda _home: None)  # pyright: ignore[reportUnknownArgumentType]
    with pytest.raises(RuntimeError, match="no registry record"):
        rotate.build_state()


def test_build_state_reads_current_settings(rotation_fixture: rotate.RotationState) -> None:
    state = rotation_fixture
    assert state.identity == _IDENTITY
    assert state.old_secret == _OLD_SECRET
    assert state.new_secret == _NEW_SECRET
    assert state.pg_port and state.redis_port
    assert state.pgbouncer_enabled is False


def test_preflight_passes_on_a_freshly_provisioned_cluster(
    rotation_fixture: rotate.RotationState,
) -> None:
    assert rotate.preflight(rotation_fixture) is True


def test_full_rotation_end_to_end(rotation_fixture: rotate.RotationState) -> None:
    state = rotation_fixture

    # before: only the OLD secret works, everywhere that matters.
    assert rotate._pg_probe(state.identity, state.pg_port, state.old_secret)
    assert not rotate._pg_probe(state.identity, state.pg_port, state.new_secret)
    assert rotate._redis_probe(state.redis_port, state.old_secret)
    assert rotate._redis_probe(state.redis_port, state.old_secret, username=state.identity)
    assert not rotate._redis_probe(state.redis_port, state.new_secret, username=state.identity)

    rotate.apply_pg_role(state)
    rotate.apply_redis_acl(state)
    rotate.apply_redis_requirepass(state)
    rotate.apply_pgbouncer(state)  # disabled — must no-op, not raise
    rotate.verify(state)  # raises on any mismatch

    # after: only the NEW secret works, everywhere that matters.
    assert rotate._pg_probe(state.identity, state.pg_port, state.new_secret)
    assert not rotate._pg_probe(state.identity, state.pg_port, state.old_secret)
    assert rotate._redis_probe(state.redis_port, state.new_secret)
    assert not rotate._redis_probe(state.redis_port, state.old_secret)
    assert rotate._redis_probe(state.redis_port, state.new_secret, username=state.identity)
    assert not rotate._redis_probe(state.redis_port, state.old_secret, username=state.identity)


def test_rotation_phases_are_idempotent(rotation_fixture: rotate.RotationState) -> None:
    """Re-running every phase twice (the resume/retry story) must be harmless."""
    state = rotation_fixture
    for _ in range(2):
        rotate.apply_pg_role(state)
        rotate.apply_redis_acl(state)
        rotate.apply_redis_requirepass(state)
    rotate.verify(state)


def test_redis_admin_password_resolves_after_a_partial_rotation(
    rotation_fixture: rotate.RotationState,
) -> None:
    """A resumed attempt's `state.old_secret` may already be stale (a prior,
    interrupted run already flipped `default`'s password to the new one) — the
    admin dial must still find whichever password currently works."""
    state = rotation_fixture
    rotate.apply_redis_acl(state)
    rotate.apply_redis_requirepass(state)
    rotate.apply_redis_acl(state)  # simulated resume — old_secret no longer works


def test_neither_secret_raises_a_clear_error(rotation_fixture: rotate.RotationState) -> None:
    state = rotation_fixture
    with redis_lib.Redis(host="127.0.0.1", port=state.redis_port, password=state.old_secret) as r:
        r.execute_command("CONFIG", "SET", "requirepass", "something-else-entirely")  # pyright: ignore[reportUnknownMemberType]
    with pytest.raises(RuntimeError, match="neither the old nor the new"):
        rotate.apply_redis_acl(state)


def test_verify_catches_a_no_op_pg_rotation(rotation_fixture: rotate.RotationState) -> None:
    """If postgres was never actually touched, verify() must fail loud rather
    than report success — a rotation that silently no-ops is the worst outcome."""
    state = rotation_fixture
    rotate.apply_redis_acl(state)
    rotate.apply_redis_requirepass(state)
    # pg_role phase deliberately skipped.
    with pytest.raises(RuntimeError, match="postgres"):
        rotate.verify(state)


def test_apply_pgbouncer_skips_cleanly_when_disabled(
    rotation_fixture: rotate.RotationState,
) -> None:
    state = rotation_fixture
    assert state.pgbouncer_enabled is False
    rotate.apply_pgbouncer(state)  # must not raise


def test_apply_pgbouncer_calls_ensure_pgbouncer_when_enabled(
    rotation_fixture: rotate.RotationState, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = rotation_fixture
    state.pgbouncer_enabled = True
    state.pgbouncer_port = 6433
    calls: list[dict[str, object]] = []
    runner_pw = "rotation-fixture-pw"
    monkeypatch.setattr(
        "cli.commands._pgbouncer.ensure_pgbouncer",
        lambda **kw: calls.append(kw) or 0,  # pyright: ignore[reportUnknownArgumentType]
    )
    monkeypatch.setattr(
        "cli.commands._pgbouncer.runner_password_from_env",
        lambda: runner_pw,  # pyright: ignore[reportUnknownArgumentType]
    )
    rotate.apply_pgbouncer(state)
    assert calls == [
        {
            "pg_port": state.pg_port,
            "listen_port": 6433,
            "db_name": state.identity,
            "role": state.identity,
            "cluster_secret": state.new_secret,
            # The rewritten userlist keeps the ava_runner entry (Task #1236) —
            # dropping it on a rotation would break every runner at its next dial.
            "runner_password": runner_pw,
        }
    ]


def test_apply_pgbouncer_raises_on_a_nonzero_return(
    rotation_fixture: rotate.RotationState, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = rotation_fixture
    state.pgbouncer_enabled = True
    state.pgbouncer_port = 6433
    monkeypatch.setattr("cli.commands._pgbouncer.ensure_pgbouncer", lambda **_: 1)  # pyright: ignore[reportUnknownArgumentType]
    with pytest.raises(RuntimeError, match="ensure_pgbouncer failed"):
        rotate.apply_pgbouncer(state)


def test_write_env_upserts_secret_and_urls_preserving_other_lines(
    tmp_path: Path, rotation_fixture: rotate.RotationState, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = rotation_fixture
    env_path = tmp_path / ".env"
    env_path.write_text("ANTHROPIC_API_KEY=sk-unrelated\nAVA_MACHINE_NAME=test-box\n")
    monkeypatch.setattr(rotate, "AVA_ENV_PATH", env_path)

    rotate.write_env(state)

    written = dict(line.split("=", 1) for line in env_path.read_text().splitlines() if "=" in line)
    assert written["AVA_CLUSTER_SECRET"] == state.new_secret
    assert state.new_secret in written["AVA_DB_URL"]
    assert state.new_secret in written["AVA_REDIS_URL"]
    assert state.old_secret not in written["AVA_DB_URL"]
    # unrelated lines survive untouched.
    assert written["ANTHROPIC_API_KEY"] == "sk-unrelated"
    assert written["AVA_MACHINE_NAME"] == "test-box"
    # upsert_env's own snapshot_env backed up the pre-rotation .env.
    backups = list((tmp_path / "backups" / "env").glob(".env.*"))
    assert len(backups) == 1
    assert "ANTHROPIC_API_KEY=sk-unrelated" in backups[0].read_text()


def test_rotation_state_round_trips_through_disk_at_0600(
    tmp_path: Path, rotation_fixture: rotate.RotationState, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(rotate, "ava_home", lambda: tmp_path)
    state = rotation_fixture

    saved_path = state.save()

    assert saved_path.exists()
    assert oct(saved_path.stat().st_mode)[-3:] == "600"
    reloaded = rotate.RotationState.load(saved_path)
    assert reloaded == state
    # same started_at -> a second save overwrites the same file, not a new one.
    state.phase = "pg_role"
    assert state.save() == saved_path
    assert len(list(saved_path.parent.glob("rotate-*.json"))) == 1


def test_main_dry_run_never_mutates(
    rotation_fixture: rotate.RotationState,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def _boom(_state: rotate.RotationState) -> None:
        raise AssertionError("a dry run must never call a mutating phase")

    monkeypatch.setattr(rotate, "apply_pg_role", _boom)
    monkeypatch.setattr(rotate, "apply_redis_acl", _boom)
    monkeypatch.setattr(rotate, "apply_redis_requirepass", _boom)
    monkeypatch.setattr(rotate, "apply_pgbouncer", _boom)
    monkeypatch.setattr(rotate, "write_env", _boom)
    monkeypatch.setattr(rotate, "build_state", lambda: rotation_fixture)

    rc = rotate.main([])

    assert rc == 0
    assert "dry-run" in capsys.readouterr().out


def test_main_execute_writes_recovery_state_and_resume_completes(
    rotation_fixture: rotate.RotationState,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(rotate, "ava_home", lambda: tmp_path)
    monkeypatch.setattr(rotate, "build_state", lambda: rotation_fixture)
    # write_env must not touch the shared test home's .env — it would leak the
    # throwaway cluster's URLs into sibling bootstrap tests reading that file
    # (order-dependent flake, exposed 2026-08-03).
    monkeypatch.setattr(rotate, "AVA_ENV_PATH", tmp_path / ".env")

    original_apply_redis_requirepass = rotate.apply_redis_requirepass

    def _boom(_state: rotate.RotationState) -> None:
        raise RuntimeError("boom")

    monkeypatch.setattr(rotate, "apply_redis_requirepass", _boom)
    rc = rotate.main(["--execute", "--yes"])

    assert rc == 1
    err = capsys.readouterr().err
    assert "ROTATION FAILED at phase 'redis_acl'" in err
    assert "--resume" in err
    [state_file] = tmp_path.glob("backups/secret-rotation/rotate-*.json")
    saved = json.loads(state_file.read_text())
    assert saved["phase"] == "redis_acl"
    assert saved["new_secret"] == rotation_fixture.new_secret

    # restore the real phase before resuming — a real recovery run would not
    # carry the induced failure. ava_home/build_state stay patched: resume
    # still needs to dial the fixture's throwaway pg/redis and save state
    # under tmp_path.
    monkeypatch.setattr(rotate, "apply_redis_requirepass", original_apply_redis_requirepass)

    rc = rotate.main(["--execute", "--yes", "--resume", str(state_file)])
    assert rc == 0
    rotate.verify(rotation_fixture)
