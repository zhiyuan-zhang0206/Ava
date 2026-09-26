"""Real bring-up of a per-cluster Postgres+Redis instance.

Exercises cli.commands._cluster_instance end to end: initdb a fresh per-cluster
Postgres under a temp $AVA_HOME, start it on an ephemeral port with the
always-authenticated posture (peer for the OS user on the owner-only socket,
SCRAM for every other role), start a per-cluster Redis with requirepass = an
independent Redis-admin password, provision the NOLOGIN owner + db + schema
(the `ava_tinst` identifier here, passed as data — names-as-data), and check
the runtime Redis identity. Write generations and the pooler are covered by
tests/lifecycle/db_authority/test_single_box.py; the rest of the suite mocks
the bring-up out.
"""

from __future__ import annotations

import getpass
import socket
import subprocess
import tempfile
import time
from collections.abc import Iterator
from pathlib import Path
from typing import cast

import psycopg
import pytest
import redis
from redis.backoff import NoBackoff
from redis.retry import Retry

from cli.commands import _cluster_instance as ci
from cli.commands._data_plane import ensure_gateway_data_plane
from cli.commands._pgbouncer import stop_pgbouncer
from shared import cluster
from shared.cluster import provision_database
from shared.config import settings

_BEARER = "test_bearer_abc123"
_REDIS_ADMIN = "test_redis_admin_abc123"
_REDIS_RUNTIME = "test_redis_runtime_abc123"


def _gateway_config(monkeypatch: pytest.MonkeyPatch, ports: tuple[int, int]) -> Path:
    """Configure a born gateway with different Postgres and Redis usernames."""
    pg_port, redis_port = ports
    home = Path(settings.general.ava_home)
    values = {
        "AVA_DB_URL": f"postgresql://ava_main@127.0.0.1:{pg_port}/ava_main",
        "AVA_REDIS_URL": f"redis://ava:{_REDIS_RUNTIME}@127.0.0.1:{redis_port}/0",
        "AVA_REDIS_ADMIN_PASSWORD": _REDIS_ADMIN,
        "AVA_REDIS_PASSWORD": _REDIS_RUNTIME,
    }
    for key, value in values.items():
        monkeypatch.setenv(key, value)
    for field in ("db_url", "redis_url", "redis_admin_password"):
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
    (home / ".env").write_text("".join(f"{key}={value}\n" for key, value in values.items()))
    return home


def _born_intent(home: Path) -> None:
    """A first start in progress (`configured`): the bring-up's initialization
    authority. A home past it without a database authority ledger is refused."""
    import json
    from dataclasses import asdict

    record = cluster.ClusterRecord(
        ports=cast("cluster.ClusterPorts", dict(cluster.LEGACY_AVA_PORTS)),
        gateway_home=str(home),
        created_at="test",
    )
    intent = {
        "version": 1,
        "home": str(home),
        "checkout": str(Path(__file__).resolve().parents[2]),
        "worktree": False,
        "roles": ["agent-runner", "gateway"],
        "config_digest": None,
        "phase": "configured",
        "record": asdict(record),
        "env": {},
    }
    (home / "start-intent.json").write_text(json.dumps(intent))
    (home / "start-intent.json").chmod(0o600)


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


def _admin(pg_port: int, database: str) -> psycopg.Connection[tuple[object, ...]]:
    """The OS-user administrator over the owner-only socket (peer)."""
    return psycopg.connect(
        host=str(ci._pg_socket_dir()), port=pg_port, dbname=database, autocommit=True
    )


def test_per_cluster_instance_bringup(isolated_cluster: tuple[int, int]) -> None:
    pg_port, redis_port = isolated_cluster
    bearer = settings.data_plane.cluster_secret

    rc = ci.ensure_cluster_storage(
        pg_port=pg_port,
        redis_port=redis_port,
        cluster_secret=bearer,
        redis_admin_password=_REDIS_ADMIN,
        redis_password=_REDIS_RUNTIME,
        redis_user="ava_tinst",
    )
    assert rc == 0

    # Provision the NOLOGIN owner + db + schema against the cluster's own
    # instance, as the administrator over its owner-only socket. The
    # identifier is passed as data.
    provision_database(
        "ava_tinst",
        base_admin_url=ci.pg_admin_url(pg_port),
        expected_data_dir=Path(settings.general.ava_home) / "pg",
    )
    cluster.ensure_checkpoint_schema(
        "ava_tinst",
        base_admin_url=ci.pg_admin_url(pg_port),
        database_created=True,
        expected_data_dir=Path(settings.general.ava_home) / "pg",
    )

    with _admin(pg_port, "ava_tinst") as conn:
        # The owner can never log in and carries no password.
        assert conn.execute(
            "SELECT rolcanlogin, rolpassword IS NULL FROM pg_authid WHERE rolname = 'ava_tinst'"
        ).fetchone() == (False, True)
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

    # No password-less or owner door over TCP (the lock that keeps other clusters out).
    for user, password in (("ava_tinst", ""), ("ava_tinst", "wrong"), (getpass.getuser(), "")):
        with pytest.raises(psycopg.OperationalError):
            psycopg.connect(
                host="127.0.0.1", port=pg_port, user=user, password=password, dbname="ava_tinst"
            )

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
            ci.ensure_cluster_storage(
                pg_port=pg_port,
                redis_port=redis_port,
                cluster_secret=bearer,
                redis_admin_password=_REDIS_ADMIN,
                redis_password=_REDIS_RUNTIME,
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
    _born_intent(home)
    assert ensure_gateway_data_plane() == 0
    provision_database("ava_main", base_admin_url=ci.pg_admin_url(pg_port))
    with _admin(pg_port, "ava_main") as conn:
        assert conn.execute("SELECT current_database()").fetchone() == ("ava_main",)
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


def test_fresh_single_box_redis_refuses_unauthenticated_connections(
    isolated_cluster: tuple[int, int], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A fresh single-box home (empty bearer) is born with generated Redis
    credentials, and the official bring-up serves a Redis that refuses an
    unauthenticated client; the runtime ACL user and the admin user authenticate."""
    from cli.start_identity import IdentityInput, _gateway_values

    pg_port, redis_port = isolated_cluster
    home = Path(settings.general.ava_home)
    ports = cluster.LEGACY_AVA_PORTS.copy()
    ports.update(postgres=pg_port, redis=redis_port, pgbouncer=_free_port())
    record = cluster.ClusterRecord(
        ports=cast("cluster.ClusterPorts", ports), gateway_home=str(home), created_at="test"
    )
    values = _gateway_values(
        record,
        IdentityInput(
            home,
            tmp_path / "clusters.json",
            tmp_path,
            False,
            frozenset({"gateway", "agent-runner"}),
            {"AVA_PGBOUNCER_ENABLED": "false"},
        ),
    )
    assert values["AVA_CLUSTER_SECRET"] == ""
    admin = values["AVA_REDIS_ADMIN_PASSWORD"]
    (home / ".env").write_text("".join(f"{key}={value}\n" for key, value in values.items()))
    monkeypatch.setattr(settings.data_plane, "cluster_secret", "")
    monkeypatch.setattr(settings.data_plane, "db_url", values["AVA_DB_URL"])
    monkeypatch.setattr(settings.data_plane, "redis_url", values["AVA_REDIS_URL"])
    monkeypatch.setattr(settings.data_plane, "redis_admin_password", admin)
    monkeypatch.setattr(settings.data_plane, "events_channel", values["AVA_EVENTS_CHANNEL"])

    def _record(_home: Path) -> cluster.ClusterRecord:
        return record

    monkeypatch.setattr(cluster, "get_record", _record)
    _born_intent(home)
    try:
        assert ensure_gateway_data_plane() == 0
        with (
            redis.Redis(port=redis_port, retry=Retry(NoBackoff(), 0)) as anonymous,
            pytest.raises(redis.AuthenticationError),
        ):
            anonymous.ping()  # pyright: ignore[reportUnknownMemberType]
        with redis.Redis.from_url(values["AVA_REDIS_URL"]) as runtime:  # pyright: ignore[reportUnknownMemberType]
            assert runtime.ping()  # pyright: ignore[reportUnknownMemberType]
        with redis.Redis(port=redis_port, password=admin) as administrator:
            assert administrator.ping()  # pyright: ignore[reportUnknownMemberType]
        assert f'requirepass "{admin}"' in (home / "redis" / "redis.conf").read_text()
    finally:
        subprocess.run(  # noqa: S603
            [ci._redis_cli_bin(), "-p", str(redis_port), "shutdown", "nosave"],
            env=ci._redis_cli_env(admin),
            check=False,
            capture_output=True,
        )


def test_unix_only_foreign_postgres_same_port_cannot_receive_provisioning(
    isolated_cluster: tuple[int, int],
) -> None:
    """TCP ownership does not make another directory's equal Unix port ours."""
    pg_port, _redis_port = isolated_cluster
    assert ci._start_pg(pg_port, "") == 0
    owned_data = Path(settings.general.ava_home) / "pg"
    with tempfile.TemporaryDirectory(prefix="ava-pg-foreign-", dir="/tmp") as temporary:
        directory = Path(temporary)
        data = directory / "data"
        ci._initdb(data)
        subprocess.run(  # noqa: S603 — private test-owned postgres
            [
                ci._pg_bin("pg_ctl"),
                "-D",
                str(data),
                "-l",
                str(directory / "pg.log"),
                "-w",
                "start",
                "-o",
                f"-p {pg_port} -c listen_addresses='' "
                f"-c unix_socket_directories={directory} {ci.pg_shm_args()}",
            ],
            check=True,
            capture_output=True,
            env=ci.pg_start_env(),
        )
        try:
            foreign_url = (
                f"postgresql://{getpass.getuser()}@/postgres?host={directory}&port={pg_port}"
            )
            canonical_socket = ci._pg_socket_dir() / f".s.PGSQL.{pg_port}"
            canonical_socket.unlink()
            assert ci._pg_running(pg_port), "the correct TCP instance is still ready"
            with pytest.raises(psycopg.OperationalError):
                cluster.ensure_cluster_role(
                    "must_not_create",
                    base_admin_url=ci.pg_admin_url(pg_port),
                    expected_data_dir=owned_data,
                )
            with pytest.raises(RuntimeError, match="foreign Unix socket"):
                cluster.ensure_cluster_role(
                    "must_not_create",
                    base_admin_url=foreign_url,
                    expected_data_dir=owned_data,
                )
            # Even the expected pathname cannot authorize a foreign backend.
            canonical_socket.symlink_to(directory / f".s.PGSQL.{pg_port}")
            with pytest.raises(RuntimeError, match="backend is not owned"):
                cluster.ensure_cluster_role(
                    "must_not_create",
                    base_admin_url=ci.pg_admin_url(pg_port),
                    expected_data_dir=owned_data,
                )
            with psycopg.connect(foreign_url) as conn:
                assert (
                    conn.execute(
                        "SELECT 1 FROM pg_roles WHERE rolname = 'must_not_create'"
                    ).fetchone()
                    is None
                )
        finally:
            subprocess.run(  # noqa: S603 — private test-owned postgres
                [ci._pg_bin("pg_ctl"), "-D", str(data), "-m", "immediate", "stop"],
                check=True,
                capture_output=True,
            )


# ─── Task #1113: first-install hba timing + ambient-secret leak ──────────────
#
# The bug chain: install-time birth starts pg BEFORE the cluster's .env exists,
# so an hba written from ambient `settings` followed an inherited sibling
# secret, and a rewritten hba was never reloaded into the running server. The
# fix: the hba/bind follow the caller-passed cluster secret (never ambient
# settings), and a rewritten hba is reloaded and PROVEN enforced before start
# proceeds (`require_authenticated_hba`).


def _active_hba(pg_port: int) -> list[tuple[str, str]]:
    """(address, auth_method) of the host lines in the hba file, in file order."""
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
    a sibling cluster's secret: the hba has loopback SCRAM lines only (no
    reachable/cidr reach) and the running server refuses a password-less TCP
    dial even for the OS-user superuser."""
    pg_port, redis_port = isolated_cluster
    rc = ci.ensure_cluster_storage(
        pg_port=pg_port,
        redis_port=redis_port,
        cluster_secret="",
        redis_admin_password=_REDIS_ADMIN,
        redis_password=_REDIS_RUNTIME,
        redis_user="ava_tinst",
    )
    assert rc == 0
    assert _active_hba(pg_port) == [("127.0.0.1", "scram-sha-256"), ("::1", "scram-sha-256")]
    with pytest.raises(psycopg.OperationalError, match="password"):
        psycopg.connect(
            host="127.0.0.1", port=pg_port, user=getpass.getuser(), password="", dbname="postgres"
        )


def test_rewritten_hba_is_reloaded_into_running_server(
    isolated_cluster: tuple[int, int],
) -> None:
    """A running server keeps its last-loaded hba until reloaded. A postmaster
    that still serves a legacy trust hba must, after `_start_pg` rewrites the
    file, enforce passwords before start proceeds — the loaded proof, not the
    file on disk."""
    pg_port, _redis_port = isolated_cluster
    assert ci._start_pg(pg_port, "") == 0
    data = Path(settings.general.ava_home) / "pg"
    # A legacy postmaster: trust everywhere, reloaded into the running server.
    (data / "pg_hba.conf").write_text("local all all trust\nhost all all 127.0.0.1/32 trust\n")
    with psycopg.connect(ci.pg_admin_url(pg_port), autocommit=True) as conn:
        conn.execute("SELECT pg_reload_conf()")
    deadline = time.monotonic() + 10
    while True:
        try:
            psycopg.connect(
                host="127.0.0.1", port=pg_port, user=getpass.getuser(), dbname="postgres"
            ).close()
            break
        except psycopg.OperationalError:
            assert time.monotonic() < deadline, "legacy trust hba never loaded"
            time.sleep(0.05)

    assert ci._start_pg(pg_port, "") == 0
    with pytest.raises(psycopg.OperationalError, match="password"):
        psycopg.connect(
            host="127.0.0.1", port=pg_port, user=getpass.getuser(), password="", dbname="postgres"
        )


def test_hba_proof_refuses_a_postmaster_still_serving_trust(
    isolated_cluster: tuple[int, int], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The loaded-hba proof is behavioral: a postmaster that still admits a
    password-less dial (a trust line loaded) fails the proof, whatever file is
    on disk."""
    pg_port, _redis_port = isolated_cluster
    assert ci._start_pg(pg_port, "") == 0
    ci.require_authenticated_hba(pg_port, "127.0.0.1")
    data = Path(settings.general.ava_home) / "pg"
    (data / "pg_hba.conf").write_text("local all all trust\nhost all all 127.0.0.1/32 trust\n")
    with psycopg.connect(ci.pg_admin_url(pg_port), autocommit=True) as conn:
        conn.execute("SELECT pg_reload_conf()")
    monkeypatch.setattr(ci, "_HBA_PROOF_TIMEOUT_S", 1.0)
    with pytest.raises(RuntimeError, match="does not enforce password authentication"):
        ci.require_authenticated_hba(pg_port, "127.0.0.1")
