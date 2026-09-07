"""Real bring-up of a per-cluster Postgres+Redis instance.

Exercises cli.commands._cluster_instance end to end: initdb a fresh per-cluster
Postgres under a temp $AVA_HOME, start it on an ephemeral port with the
socket-trust / TCP-scram posture, start a per-cluster Redis with requirepass =
an independent Redis-admin password, provision the role+db+schema, then connect over the runtime
identities (the `ava_tinst` identifier here, passed as data — names-as-data) and
run a trivial command. This is the bring-up install-time birth takes for every
cluster; the rest of the suite mocks it out.
"""

from __future__ import annotations

import getpass
import shutil
import socket
import subprocess
from collections.abc import Iterator
from pathlib import Path
from typing import cast

import psycopg
import pytest
import redis

from cli.commands import _cluster_instance as ci
from cli.commands._data_plane import ensure_gateway_data_plane
from cli.commands._data_plane_admin_secrets import ensure_data_plane_admin_secrets
from cli.commands._pgbouncer import pgbouncer_bin, stop_pgbouncer
from shared import cluster
from shared.cluster import provision_database
from shared.config import settings

_BEARER = "test_bearer_abc123"
_DB_ADMIN = "test_db_admin_abc123"
_REDIS_ADMIN = "test_redis_admin_abc123"
_REDIS_RUNTIME = "test_redis_runtime_abc123"


def _gateway_config(
    monkeypatch: pytest.MonkeyPatch, ports: tuple[int, int], *, legacy: bool = False
) -> Path:
    """Configure a born gateway with different Postgres and Redis usernames."""
    pg_port, redis_port = ports
    home = Path(settings.general.ava_home)
    values = {
        "AVA_DB_URL": f"postgresql://ava_main:{_BEARER if legacy else _DB_ADMIN}@127.0.0.1:{pg_port}/ava_main",
        "AVA_REDIS_URL": f"redis://ava:{_BEARER if legacy else _REDIS_RUNTIME}@127.0.0.1:{redis_port}/0",
        "AVA_DB_ADMIN_PASSWORD": "" if legacy else _DB_ADMIN,
        "AVA_REDIS_ADMIN_PASSWORD": "" if legacy else _REDIS_ADMIN,
        "AVA_REDIS_PASSWORD": "" if legacy else _REDIS_RUNTIME,
    }
    for key, value in values.items():
        monkeypatch.setenv(key, value)
    for field in ("db_url", "redis_url", "db_admin_password", "redis_admin_password"):
        monkeypatch.setattr(settings.data_plane, field, values[f"AVA_{field.upper()}"])
    monkeypatch.setattr(settings.data_plane, "pgbouncer_enabled", False)
    record = cluster.ClusterRecord(
        ports=cast(
            "cluster.ClusterPorts",
            {"postgres": pg_port, "redis": redis_port, "pgbouncer": _free_port()},
        ),
        gateway_home=str(home),
        created_at="test",
    )

    def _record(_home: Path) -> cluster.ClusterRecord:
        return record

    monkeypatch.setattr(cluster, "get_record", _record)
    # The split module imports this registry seam at module load time.
    monkeypatch.setattr("cli.commands._data_plane_admin_secrets.get_record", _record)
    (home / ".env").write_text("".join(f"{key}={value}\n" for key, value in values.items()))
    return home


def _pgbouncer_installed() -> bool:
    binary = pgbouncer_bin()
    return Path(binary).exists() or shutil.which(binary) is not None


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture()
def isolated_cluster(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[int, int]]:
    """A temp $AVA_HOME + registry + cluster identity, yielding (pg_port,
    redis_port). Tears the instance down on exit."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(settings.general, "ava_home", str(home))
    monkeypatch.setattr(settings.general, "cluster_registry", str(tmp_path / "clusters.json"))
    monkeypatch.setattr(settings.data_plane, "cluster_secret", _BEARER)
    monkeypatch.setattr(settings.data_plane, "db_admin_password", _DB_ADMIN)
    monkeypatch.setattr(settings.data_plane, "redis_admin_password", _REDIS_ADMIN)
    monkeypatch.setattr(settings.data_plane, "events_channel", "ava:tinst:events")
    pg_port, redis_port = _free_port(), _free_port()
    try:
        yield pg_port, redis_port
    finally:
        # Stop the pooler first (no-op if a test never enabled it). Unlike the pg
        # and redis teardowns below — which address captured locals, a temp data
        # dir and an ephemeral port — `stop_pgbouncer` re-resolves ava_home() at
        # call time. Re-pin it rather than assume the setup patch is still in
        # force: a body that unwound it would aim this at the operator's real
        # `~/.ava` and take down the production pooler.
        monkeypatch.setattr(settings.general, "ava_home", str(home))
        stop_pgbouncer()
        subprocess.run(  # noqa: S603
            [ci._pg_bin("pg_ctl"), "-D", str(home / "pg"), "-m", "immediate", "stop"],
            check=False,
            capture_output=True,
        )
        subprocess.run(  # noqa: S603
            [ci._redis_cli_bin(), "-p", str(redis_port), "shutdown", "nosave"],
            env=ci._redis_cli_env(_REDIS_ADMIN),  # never `-a` — argv is public
            check=False,
            capture_output=True,
        )


def test_per_cluster_instance_bringup(isolated_cluster: tuple[int, int]) -> None:
    pg_port, redis_port = isolated_cluster
    bearer = settings.data_plane.cluster_secret

    rc = ci.ensure_cluster_instance(
        pg_port=pg_port,
        redis_port=redis_port,
        cluster_secret=bearer,
        db_admin_password=_DB_ADMIN,
        redis_admin_password=_REDIS_ADMIN,
        redis_password=_REDIS_RUNTIME,
        pgbouncer_port=pg_port + 1,
        identity="ava_tinst",
        redis_user="ava_tinst",
    )
    assert rc == 0

    # Provision the role + db + schema against the cluster's own instance (over its
    # local socket, trust) — exactly what cluster_lifecycle._provision does. The
    # identifier is passed as data.
    provision_database(
        "ava_tinst", base_admin_url=ci.pg_admin_url(pg_port), db_admin_password=_DB_ADMIN
    )

    # Runtime Postgres identity: the ava_tinst role over TCP scram (a co-located
    # cluster could reach the port, so the password is required).
    runtime_db = f"postgresql://ava_tinst:{_DB_ADMIN}@127.0.0.1:{pg_port}/ava_tinst"
    with psycopg.connect(runtime_db) as conn:
        row = conn.execute("SELECT 1").fetchone()
        assert row is not None and row[0] == 1
        # mmap-backed shared memory pinned by `_start_pg` (Task #1263): the main
        # region and dynamic segments live in files under the data dir, not POSIX
        # shm in /dev/shm, so an external unlink of /dev/shm cannot take the
        # instance down. Live check — this runs on Linux CI and macOS alike.
        for name in ("shared_memory_type", "dynamic_shared_memory_type"):
            row = conn.execute(
                "SELECT setting FROM pg_settings WHERE name = %s", (name,)
            ).fetchone()
            assert row is not None and row[0] == "mmap"
        # the schema applied (schema_migrations is the last table created)
        assert conn.execute(
            "SELECT 1 FROM information_schema.tables WHERE table_name = 'schema_migrations'"
        ).fetchone()

    # A wrong password is rejected over TCP (the lock that keeps other clusters out).
    with pytest.raises(psycopg.OperationalError):
        psycopg.connect(f"postgresql://ava_tinst:wrong@127.0.0.1:{pg_port}/ava_tinst")
    with pytest.raises(psycopg.OperationalError):
        psycopg.connect(f"postgresql://ava_tinst:{bearer}@127.0.0.1:{pg_port}/ava_tinst")

    # Runtime Redis identity: the ava_tinst ACL user.
    # redis-py's from_url carries **kwargs: Unknown in its stub.
    r = redis.Redis.from_url(  # pyright: ignore[reportUnknownMemberType]
        f"redis://ava_tinst:{_REDIS_RUNTIME}@127.0.0.1:{redis_port}/0"
    )
    assert r.ping()  # pyright: ignore[reportUnknownMemberType]
    r.close()
    with (
        redis.Redis.from_url(  # pyright: ignore[reportUnknownMemberType]
            f"redis://ava_tinst:{bearer}@127.0.0.1:{redis_port}/0"
        ) as wrong_bearer,
        pytest.raises(redis.AuthenticationError),
    ):
        wrong_bearer.ping()  # pyright: ignore[reportUnknownMemberType]


def test_bringup_is_idempotent(isolated_cluster: tuple[int, int]) -> None:
    """A second ensure on an already-running instance is a no-op success (the warm
    `ava start` path)."""
    pg_port, redis_port = isolated_cluster
    bearer = settings.data_plane.cluster_secret
    for _ in range(2):
        assert (
            ci.ensure_cluster_instance(
                pg_port=pg_port,
                redis_port=redis_port,
                cluster_secret=bearer,
                db_admin_password=_DB_ADMIN,
                redis_admin_password=_REDIS_ADMIN,
                redis_password=_REDIS_RUNTIME,
                pgbouncer_port=pg_port + 1,
                identity="ava_tinst",
                redis_user="ava_tinst",
            )
            == 0
        )


def test_gateway_cold_start_restores_redis_url_identity(
    isolated_cluster: tuple[int, int], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Official start restores Redis's own ACL after a real RDB-only shutdown."""
    pg_port, redis_port = isolated_cluster
    home = _gateway_config(monkeypatch, isolated_cluster)
    assert ensure_gateway_data_plane() == 0
    provision_database(
        "ava_main", base_admin_url=ci.pg_admin_url(pg_port), db_admin_password=_DB_ADMIN
    )
    with psycopg.connect(settings.data_plane.db_url) as conn:
        assert conn.execute("SELECT current_user, current_database()").fetchone() == (
            "ava_main",
            "ava_main",
        )
    with redis.Redis.from_url(settings.data_plane.redis_url) as client:  # pyright: ignore[reportUnknownMemberType]
        assert client.ping()  # pyright: ignore[reportUnknownMemberType]
        assert client.set("continuation", "pending")  # pyright: ignore[reportUnknownMemberType]
    subprocess.run(  # noqa: S603
        [ci._redis_cli_bin(), "-p", str(redis_port), "shutdown", "save"],
        env=ci._redis_cli_env(_REDIS_ADMIN),
        check=True,
        capture_output=True,
    )
    assert (home / "redis" / "dump.rdb").is_file()
    assert ensure_gateway_data_plane() == 0
    with redis.Redis.from_url(settings.data_plane.redis_url) as client:  # pyright: ignore[reportUnknownMemberType]
        assert client.get("continuation") == b"pending"  # pyright: ignore[reportUnknownMemberType]
    with redis.Redis(port=redis_port, password=_REDIS_ADMIN) as admin:
        assert admin.acl_getuser("ava") is not None  # pyright: ignore[reportUnknownMemberType]
        assert admin.acl_getuser("ava_main") is None  # pyright: ignore[reportUnknownMemberType]


def test_credential_split_preserves_distinct_redis_user(
    isolated_cluster: tuple[int, int], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Password splitting rotates the existing URL identities independently."""
    pg_port, redis_port = isolated_cluster
    _gateway_config(monkeypatch, isolated_cluster, legacy=True)
    assert ensure_gateway_data_plane() == 0
    provision_database(
        "ava_main", base_admin_url=ci.pg_admin_url(pg_port), db_admin_password=_BEARER
    )
    # Model the existing live ACL that lets a legacy cluster serve before its
    # first password split; this is real provisioning, not a mocked Redis client.
    cluster.ensure_cluster_redis_acl(
        "ava",
        redis_admin_url=f"redis://default:{_BEARER}@127.0.0.1:{redis_port}",
        runtime_password=_BEARER,
        channel_prefix=settings.data_plane.events_channel.removesuffix(":events"),
    )
    minted = iter((_DB_ADMIN, _REDIS_ADMIN, _REDIS_RUNTIME))

    def _token_urlsafe(_bytes: int) -> str:
        return next(minted)

    monkeypatch.setattr(
        "cli.commands._data_plane_admin_secrets.secrets.token_urlsafe", _token_urlsafe
    )
    assert ensure_data_plane_admin_secrets() is True
    with psycopg.connect(settings.data_plane.db_url) as conn:
        assert conn.execute("SELECT current_user").fetchone() == ("ava_main",)
    with redis.Redis.from_url(settings.data_plane.redis_url) as client:  # pyright: ignore[reportUnknownMemberType]
        assert client.ping()  # pyright: ignore[reportUnknownMemberType]
    with (
        redis.Redis(port=redis_port, username="ava", password=_BEARER) as old_runtime,
        pytest.raises(redis.AuthenticationError),
    ):
        old_runtime.ping()  # pyright: ignore[reportUnknownMemberType]


@pytest.mark.skipif(not _pgbouncer_installed(), reason="pgbouncer not installed (brew/apt)")
def test_bringup_with_pgbouncer_enabled(
    isolated_cluster: tuple[int, int], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fresh-cluster full-pooled bring-up (the prod default posture): ensure_cluster_instance
    starts pg + redis + PgBouncer on the REAL trust unix socket, migrations run DIRECT (the
    advisory-lock exemption) while the pooler is up, and a runtime consumer reaches Postgres
    THROUGH PgBouncer via the one AVA_DB_URL (which carries the pooler port when pooling is
    on), while connect(direct=True) reaches the real Postgres via the registry-derived direct
    URL. Exercises the socket hop the wire test (TCP) cannot, and proves the direct exemption
    is actually taken with pooling on."""
    pg_port, redis_port = isolated_cluster
    dp = settings.data_plane
    bearer = dp.cluster_secret
    pgb_port = _free_port()
    monkeypatch.setattr(dp, "pgbouncer_enabled", True)

    rc = ci.ensure_cluster_instance(
        pg_port=pg_port,
        redis_port=redis_port,
        cluster_secret=bearer,
        db_admin_password=_DB_ADMIN,
        redis_admin_password=_REDIS_ADMIN,
        redis_password=_REDIS_RUNTIME,
        pgbouncer_port=pgb_port,
        identity="ava_tinst",
        redis_user="ava_tinst",
    )
    assert rc == 0

    # Provision role+db+schema (direct, socket superuser) — as cluster_lifecycle._provision does.
    provision_database(
        "ava_tinst", base_admin_url=ci.pg_admin_url(pg_port), db_admin_password=_DB_ADMIN
    )

    # On macOS, PgBouncer 1.25.x enters a broken state after its first backend
    # connection fails (role does not exist yet). A full restart after provisioning
    # clears the state so subsequent pooled connections open fresh server links.
    import time as _time

    from cli.commands._pgbouncer import ensure_pgbouncer as _ensure_pgb

    stop_pgbouncer()
    _time.sleep(0.2)
    _ensure_pgb(
        pg_port=pg_port,
        listen_port=pgb_port,
        db_name="ava_tinst",
        role="ava_tinst",
        cluster_secret=bearer,
        db_admin_password=_DB_ADMIN,
    )

    # The one URL: AVA_DB_URL itself carries the pooler port (pooling on). The
    # scratch cluster has no registry record, so hand direct_db_url() the record
    # it would read on a real gateway (pg port + pooler port of this instance).
    from shared import cluster as _cl
    from shared import paths as _paths

    pooled_db = f"postgresql://ava_tinst:{_DB_ADMIN}@127.0.0.1:{pgb_port}/ava_tinst"
    direct_db = f"postgresql://ava_tinst:{_DB_ADMIN}@127.0.0.1:{pg_port}/ava_tinst"
    monkeypatch.setattr(dp, "db_url", pooled_db)
    monkeypatch.setattr(_paths, "ava_home", lambda: Path("/x/.ava-tinst"))
    monkeypatch.setattr(
        _cl,
        "load_registry",
        lambda: {
            "/x/.ava-tinst": _cl.ClusterRecord(
                ports=cast("_cl.ClusterPorts", {"postgres": pg_port, "pgbouncer": pgb_port}),
                gateway_home="/x/.ava-tinst",
                created_at="t",
            )
        },
    )

    import shared.db
    from shared.migrations import apply_pending_migrations

    # Migrations take the DIRECT exemption (session advisory lock) even with the pooler up.
    with shared.db.connect(direct=True) as conn:
        apply_pending_migrations(conn)

    # A runtime consumer reaches Postgres THROUGH PgBouncer (client scram + trust socket hop).
    with shared.db.connect() as conn:  # dp.db_url — the one access URL
        row = conn.execute("SELECT 1").fetchone()
        assert row is not None and row[0] == 1
        assert conn.execute(
            "SELECT 1 FROM information_schema.tables WHERE table_name = 'schema_migrations'"
        ).fetchone()

    # The direct exemption really dials the real Postgres (not the pooler).
    assert shared.db.direct_db_url() == direct_db
    with shared.db.connect(direct=True) as conn:
        row = conn.execute("SELECT 1").fetchone()
        assert row is not None and row[0] == 1


# ─── Task #1113: first-install hba timing + ambient-secret leak ──────────────
#
# The bug chain (reproduced on main): install-time birth starts pg BEFORE the
# cluster's .env exists, so the hba is written from whatever `settings` sees —
# an inherited sibling secret (a prod-sourced shell) wrote scram lines keyed to
# a FOREIGN secret into a no-secret cluster's hba, and the first `ava start`
# rewrote the file but never reloaded the running server, which kept serving
# the stale hba → the migration applier's passwordless direct dial failed
# `fe_sendauth: no password supplied`. The fix: the hba/bind follow the
# caller-passed cluster secret (never ambient settings), and a rewritten hba is
# reloaded into a running server immediately.


def _active_hba(pg_port: int) -> list[tuple[str, str]]:
    """(address, auth_method) of the RUNNING server's host lines, in file order —
    what pg actually enforces, not what is on disk."""
    with psycopg.connect(ci.pg_admin_url(pg_port)) as conn:
        rows = conn.execute(
            "SELECT address, auth_method FROM pg_hba_file_rules "
            "WHERE type = 'host' ORDER BY line_number"
        ).fetchall()
    return [(str(r[0]), str(r[1])) for r in rows]


def test_birth_hba_follows_passed_secret_not_ambient_settings(
    isolated_cluster: tuple[int, int],
) -> None:
    """Install-time birth of a no-secret cluster while the ambient settings carry
    a sibling cluster's secret: the hba (disk AND active) must be trust-only, and
    a passwordless TCP dial — the migration applier's connection — must succeed.

    The fixture's ambient `cluster_secret` is `test_secret_abc123`, so a bring-up
    that read settings instead of the passed secret would write scram and fail
    the passwordless dial below."""
    pg_port, redis_port = isolated_cluster
    rc = ci.ensure_cluster_instance(
        pg_port=pg_port,
        redis_port=redis_port,
        cluster_secret="",
        pgbouncer_port=pg_port + 1,
        identity="ava_tinst",
        redis_user="ava_tinst",
    )
    assert rc == 0

    # The running server enforces trust on TCP (the no-secret posture).
    assert _active_hba(pg_port) == [
        ("127.0.0.1", "trust"),
        ("::1", "trust"),
    ]
    with psycopg.connect(f"postgresql://{getpass.getuser()}@127.0.0.1:{pg_port}/postgres") as conn:
        row = conn.execute("SELECT 1").fetchone()
        assert row is not None and row[0] == 1


def test_rewritten_hba_is_reloaded_into_running_server(
    isolated_cluster: tuple[int, int],
) -> None:
    """The first `ava start` after birth rewrites the hba (the .env now exists and
    the cluster's real secret is known). A running server keeps its last-loaded
    copy until reloaded — without the reload the migration applier would keep
    seeing the STALE hba: for the install→first-start handover of a secret
    cluster, the old trust hba stays active (posture unenforced); for a no-secret
    cluster born under a leaked secret, the old scram hba stays active and the
    passwordless migration dial fails. The rewritten hba must be reloaded in."""
    pg_port, _redis_port = isolated_cluster

    # Phase 1: install-time birth — no .env yet, cluster secret "".
    assert ci._start_pg(pg_port, "") == 0
    assert _active_hba(pg_port) == [("127.0.0.1", "trust"), ("::1", "trust")]

    # Phase 2: first `ava start` — the cluster's real secret is now known. The
    # file is rewritten to scram AND must be ACTIVE immediately (the reload).
    assert ci._start_pg(pg_port, "s3cret") == 0
    assert _active_hba(pg_port) == [
        ("127.0.0.1", "scram-sha-256"),
        ("::1", "scram-sha-256"),
    ]
    # The posture is now enforced: a passwordless TCP dial is refused.
    with pytest.raises(psycopg.OperationalError):
        psycopg.connect(f"postgresql://{getpass.getuser()}@127.0.0.1:{pg_port}/postgres")


def test_fresh_install_migrations_apply_no_secret(
    isolated_cluster: tuple[int, int], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The full first-install e2e (Task #1113): fresh home → install-time birth
    with an EMPTY cluster secret (single-box no-auth) under an ambient foreign
    secret → provision → first-start re-bring-up → the migration applier's
    passwordless DIRECT dial succeeds and applies every pending migration."""
    pg_port, redis_port = isolated_cluster
    secret = ""

    # install-time birth (cluster_lifecycle._birth): ambient settings carry the
    # fixture's foreign secret — the bring-up must follow the passed "".
    rc = ci.ensure_cluster_instance(
        pg_port=pg_port,
        redis_port=redis_port,
        cluster_secret=secret,
        pgbouncer_port=pg_port + 1,
        identity="ava_tinst",
        redis_user="ava_tinst",
    )
    assert rc == 0
    provision_database(
        "ava_tinst", base_admin_url=ci.pg_admin_url(pg_port), db_admin_password=secret
    )

    # first `ava start`: re-bring-up rewrites the (identical, trust) hba and
    # reloads it — still trust, still passwordless-dialable.
    rc = ci.ensure_cluster_instance(
        pg_port=pg_port,
        redis_port=redis_port,
        cluster_secret=secret,
        pgbouncer_port=pg_port + 1,
        identity="ava_tinst",
        redis_user="ava_tinst",
    )
    assert rc == 0
    assert _active_hba(pg_port) == [("127.0.0.1", "trust"), ("::1", "trust")]

    # The migration applier — the one sanctioned direct dial, passwordless on a
    # no-secret cluster — must run to completion (the W3-verify failure mode).
    dp = settings.data_plane
    monkeypatch.setattr(dp, "db_url", f"postgresql://ava_tinst@127.0.0.1:{pg_port}/ava_tinst")
    import shared.db
    from shared.migrations import apply_pending_migrations

    with shared.db.connect(direct=True, unbounded=True) as conn:
        done = apply_pending_migrations(conn)
    assert done, "expected every post-baseline migration to apply on a fresh install"
    with shared.db.connect(direct=True, unbounded=True) as conn:
        assert apply_pending_migrations(conn) == []
