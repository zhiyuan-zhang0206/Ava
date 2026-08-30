"""Throwaway Postgres + Redis for the test / eval suites — native processes, no Docker.

Each pytest-session worker gets its OWN throwaway Postgres cluster and Redis
server: the Postgres cluster comes from `shared.pg_tools.throwaway_postgres`
(a fresh `initdb` on an ephemeral localhost port), and a `redis-server` runs on
its own ephemeral port. The provisioning fixtures point `settings.data_plane.db_url` /
`settings.data_plane.redis_url` at them. No external server and no `*_DB_URL` env var is
needed — the fixture *owns* the server, torn down on context exit.

One server process per worker = the same isolation the old per-worker container
gave, minus the Docker dependency: nothing to pull, no daemon, no socket mount,
no sibling-container engine contention. Postgres is started (not mocked) because
Ava's durable inbound queue (the claim node's SELECT + recheck) and history
persistence (the LangGraph `PostgresSaver`) both live in it; neither has a
faithful in-process fake.

Both servers run with durability off and their data dir on tmpfs when available
(`/dev/shm` on Linux) — the data is disposable, so durability is pointless and
RAM-backing takes contended host disk I/O off the path (the disk stall that was
the e2e lifecycle flake).
"""

import shutil
import socket
import subprocess
import tempfile
import time
from collections.abc import Generator
from contextlib import closing, contextmanager
from pathlib import Path

import psycopg
import redis

from shared.pg_tools import throwaway_postgres

_SCHEMA_SQL = Path(__file__).resolve().parent.parent / "db" / "schema.sql"

# tmpfs base for the throwaway redis data dir: /dev/shm on Linux (RAM), else the
# OS temp dir (mac has no /dev/shm; its SSD-backed $TMPDIR is fast enough).
_TMPFS_BASE = "/dev/shm" if Path("/dev/shm").is_dir() else None  # noqa: S108 — deliberate tmpfs for throwaway server data


def _free_port() -> int:
    """Ask the OS for an unused localhost TCP port, closed immediately.

    The release-to-bind window is the same TOCTOU class `shared/pg_tools` fixed
    for throwaway Postgres (PR #1216): a parallel xdist worker can be handed the
    same port before the server binds it, and the collision surfaces as the
    spawned server dying (then `_wait_port` times out). Accepted here: the
    window is the server's own bind latency (~ms, no initdb in between), the
    allocation count is small (one per worker, not per fixture), and no caller
    retries — callers that need determinism should use the pg_tools allocator."""
    with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_port(port: int, timeout: float = 30.0, host: str = "127.0.0.1") -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as s:
            if s.connect_ex((host, port)) == 0:
                return
        time.sleep(0.05)
    raise RuntimeError(f"server on {host}:{port} did not become ready in {timeout}s")


@contextmanager
def postgres() -> Generator[str]:
    """A throwaway Postgres cluster with db/schema.sql + checkpoint tables + all
    pending migrations applied, ready for the test suite. Yields a psycopg
    connection URL.

    schema.sql stamps only the baseline sentinel; a real `ava start` then applies
    every post-baseline migration during bring-up. Mirror that here so the test
    DB's applied-set matches the code's required-set — otherwise a daemon that
    verifies the schema at startup (`assert_schema_current`, e.g. the e2e ops
    server) aborts against a DB left behind the first real post-baseline
    migration, and its port never binds. Applying migrations at provisioning time
    keeps every future migration covered without special-casing any one of them.
    """
    from shared.migrations import apply_pending_migrations

    with throwaway_postgres(schema_sql=_SCHEMA_SQL.read_text()) as url:
        # Non-autocommit conn: apply_pending_migrations manages its own
        # per-migration transactions + advisory lock.
        with psycopg.connect(url) as conn:
            apply_pending_migrations(conn)
        yield url


@contextmanager
def redis_server() -> Generator[str]:
    """Start a throwaway redis-server on an ephemeral port, yield its URL.
    The process + data dir are destroyed on context exit."""
    tmp = Path(tempfile.mkdtemp(prefix="ava-redis-", dir=_TMPFS_BASE))
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
            "--appendonly",
            "no",
            "--dir",
            str(tmp),
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        _wait_port(port)
        url = f"redis://127.0.0.1:{port}/0"
        with redis.Redis.from_url(url) as r:
            r.ping()
            # A subprocess (e2e gateway / ops daemon) loads Settings fresh, which
            # re-derives the redis_url username to the test suite's cluster ACL user
            # `ava_citest` (the suite's URL-carried identity, tests/conftest.py). This throwaway
            # redis has only the `default` user, so add `ava_citest` as a nopass
            # full-access user (the redis analog of the throwaway pg's peer-superuser
            # `ava_citest`) — otherwise that connection is rejected (WRONGPASS / no
            # such user).
            r.execute_command(
                "ACL",
                "SETUSER",
                "ava_citest",
                "on",
                "nopass",
                "~*",
                # Prod-shaped channel grants (mirrors ensure_cluster_redis_acl):
                # the wildcard `&*` masked the hosted dispatcher's need for the
                # `&ava:inbound:*` subscription-pattern grant (soak startup bug
                # 2026-08-30) — every redis consumer in this suite must live
                # under the production channel scope.
                "&ava:*",
                "&ava:inbound:*",
                "+@all",
            )
        yield url
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            # A wedged redis-server must not leak (and must not mask the
            # test's original exception): kill it, mirroring the milvus
            # fixture's terminate→wait→kill pattern in tests/conftest.py
            # (audit round-2 cc-docs-tests P2).
            proc.kill()
            proc.wait(timeout=10)
        shutil.rmtree(tmp, ignore_errors=True)
