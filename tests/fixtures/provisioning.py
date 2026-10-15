"""The suite's data plane and its session hooks.

Each requested native resource has one session owner per pytest process (each
xdist worker is its own session). Function fixtures deliver its URL through
settings, environment and the private .env, then restore those surfaces. DB
consumers drain telemetry and truncate; Redis consumers flush their server.
Unrelated tests retain unreachable sentinels and the default driver refusal.
This module also owns the session hooks that guard the run itself: the
full-run guard (`pytest_configure`), the non-test-database refusal
(`pytest_sessionstart`), and the leaked OS-job, runaway-memory and home cleanup
(`pytest_sessionfinish`).

Static processes refuse explicit resource requests; the session hooks still run.

Sync vs async connection fixtures:
- `db_conn`  (sync `psycopg.Connection`) — for root `db.py` shared helpers and
  UI related tests
- `adb_conn` (async `psycopg.AsyncConnection`) — for kernel `agent/db/__init__.py`
"""

from __future__ import annotations

import functools
import os
import re
import subprocess
import sys
from collections.abc import Callable, Iterator, Mapping
from pathlib import Path

import pytest
import pytest_asyncio

from base.cluster.dataplane.pg_throwaway_base import ThrowawayConfig, configured_base
from base.native_process.code_version import CodeVersion
from base.native_process.loaded_commit import LoadedCommit
from gateway.tests.agents.gateway_auth import install_default_gateway_credentials
from tests._os_jobs import host_ava_os_jobs
from tests._test_env_file import rewrite_line as _rewrite_test_env_file_line
from tests.fixtures.env_bootstrap import (
    TEST_AVA_HOME,
)
from tests.fixtures.static_environment import static_mode

# Host job inventory as it stood BEFORE this session — `pytest_sessionfinish`
# diffs against it and fails the run on anything new (see tests/_os_jobs.py).
_OS_JOBS_AT_START = host_ava_os_jobs()
_PROCESS_IMAGE_KEY = pytest.StashKey[LoadedCommit]()


@pytest.fixture(scope="session")
def throwaway_config(process_config) -> ThrowawayConfig:
    """Carry the session owner's storage override into every requested native cluster."""
    return ThrowawayConfig(configured_base(process_config.view.data_plane.pg_throwaway_base))


@pytest.fixture(scope="session")
def provisioned_db(
    pytestconfig: pytest.Config,
    process_config,
    database_version: CodeVersion,
    throwaway_config: ThrowawayConfig,
) -> Iterator[str]:
    """Own one native cluster only when a database consumer requests it."""
    if static_mode(pytestconfig):
        pytest.fail("Static tests cannot request Postgres; use the native test environment.")
    from langgraph.checkpoint.postgres import PostgresSaver

    from base.cluster.machine.boot import machine_from_boot
    from base.db.test_db_guard import assert_test_db_url
    from base.deploy.tests.containers import postgres

    # Resolve this entry's image before tests can replace subprocess.run.
    database_version.get()
    machine = machine_from_boot(process_config)
    with postgres(config=throwaway_config, machine_name=machine.name, watch_stalls=False) as url:
        assert_test_db_url(url, context="provisioned_db")
        process_config.set_field("db_url", url)
        with PostgresSaver.from_conn_string(url) as saver:
            saver.setup()
        yield url


@pytest.fixture(scope="session")
def provisioned_redis(pytestconfig: pytest.Config, process_config) -> Iterator[str]:
    """Own one native server only when a Redis consumer requests it."""
    if static_mode(pytestconfig):
        pytest.fail("Static tests cannot request Redis; use the native test environment.")
    from base.deploy.tests.containers import redis_server

    with redis_server() as url:
        process_config.set_field("redis_url", url)
        yield url


# Options whose value is a separate token: `pytest --ignore tests/x.py` must not
# let that value count as a positional file arg. argparse normally consumes such
# values before they reach `config.args`, but the guard must not depend on that
# (--deselect's value even looks like a file path with a `::nodeid` suffix).
_VALUE_OPTIONS = frozenset(
    {
        "--ignore",
        "--deselect",
        "--ignore-glob",
        "--rootdir",
        "--basetemp",
        "--confcutdir",
        "--cov",
        "--junitxml",
    }
)


def full_run_guard_message(
    rootpath: Path, args: list[str], env: Mapping[str, str], cwd: Path
) -> str | None:
    if env.get("GITHUB_ACTIONS") == "true":
        return None
    if env.get("AVA_ALLOW_FULL_PYTEST") == "1":
        return None

    shared_roots = {
        (Path.home() / "Ava").resolve(),
        (Path.home() / ".ava" / "source").resolve(),
    }
    resolved_rootpath = rootpath.resolve()
    if resolved_rootpath not in shared_roots:
        return None

    skip_next = False
    for arg in args:
        if skip_next:
            skip_next = False
            continue
        if arg in _VALUE_OPTIONS:
            skip_next = True
            continue
        if arg.startswith("-"):
            continue
        selected_path = Path(arg.split("::", maxsplit=1)[0])
        # pytest resolves positional args against the invocation dir, not the
        # rootdir — `cd tests && pytest ava/test_x.py` is a legit targeted run
        # whose arg only exists under cwd. Fall back to the rootdir for args
        # given from the checkout root (the common case).
        if selected_path.is_absolute():
            candidates = [selected_path]
        else:
            candidates = [cwd / selected_path, resolved_rootpath / selected_path]
        if any(candidate.is_file() for candidate in candidates):
            return None

    return """\
Full local pytest runs are blocked on this shared Ava checkout.
User ruling (2026-08-24): do not run the full suite on the shared production host.
Run a targeted test instead:
  pytest tests/<path>::<func>
Directory-only selections are blocked because they still collect too much.
Full-suite verification runs in GitHub CI when you push.
For an explicitly user-approved local full run, use:
  AVA_ALLOW_FULL_PYTEST=1 pytest ...
Do not use this override without user approval."""


def pytest_configure(config: pytest.Config) -> None:
    message = full_run_guard_message(
        config.rootpath, config.args, os.environ, config.invocation_params.dir
    )
    if message is not None:
        pytest.exit(message, returncode=1)
    if _PROCESS_IMAGE_KEY not in config.stash:
        config.stash[_PROCESS_IMAGE_KEY] = LoadedCommit.capture(Path(__file__).resolve().parents[2])


def pytest_sessionstart(session: pytest.Session) -> None:
    """Fail fast when this pytest process resolved a non-test DB URL.

    The env block in `tests.fixtures.env_bootstrap` pins AVA_DB_URL to the unreachable
    sentinel before any project import, so on the safe path
    ``settings.data_plane.db_url`` is the sentinel here. The guard's job is
    the path where that block did NOT run: a bootstrap that stops loading the
    env plugin (the 2026-08-12 incident class — a test run rooted outside this
    repo resolved the operator's real ~/.ava/.env and seeded synthetic
    agents into the production database) leaves settings pointing at whatever
    home the process resolves, and this hook refuses the whole session before
    any fixture or test runs.

    Runs in every process: each xdist worker is its own pytest session and
    re-imports this plugin, and subprocess tests inherit the pinned
    environment from their parent worker.
    """
    from tests.fixtures.env_bootstrap import UNPROVISIONED_DB_URL

    assert os.environ["AVA_DB_URL"] == UNPROVISIONED_DB_URL


# The per-test TRUNCATE list — single source of truth. Kept as a module
# constant so tests/contracts/test_lint_truncate_isolation.py can AST-parse it and fail
# when a new per-test data table is not covered (by this list, an FK-cascade
# from it, or an explicit exemption there). Singleton/infra tables
# (deployment_state, cluster_defaults, ...) are deliberately absent — see
# the guard's exemption list for the reasons.
_PER_TEST_TRUNCATE_TABLES = (
    "inbound_messages",
    "agents_meta",
    # "events" was dropped with the task #1281/#1823 cleanup (migration
    # 20260829T030000_drop-events-archive) — the event stream lives in the append-only
    # audit_events / telemetry_events tables, which tests read in time windows of their own.
    "event_dismissals",
    "agent_model_tokens_total",
    "agent_model_tokens_total_through",
    "agent_metric_file_cursors",
    "agents",
    "alert_notification_members",
    "alert_notification_groups",
    "alerts",
    "machines",
    "machine_probe",
    "machine_status_snapshot",
    "machine_units",
    "host_deploy_state",
    "schedules",
    "schedule_versions",
    "schedule_runs",
    "agent_presets",
    "mcp_clients",
    "mcp_credential_creation_receipts",
    "api_idempotency",
    "checkpoint_blobs",
    "checkpoint_writes",
    "checkpoints",
    # The understanding tree (task #3704): derived from checkpoints, and its
    # agent_id is a plain BIGINT with no FK path into the tables above — a
    # leaked row would survive into the next test (a stale layer in a
    # run-timeline read).
    "understanding_nodes",
    "understanding_chunk_jobs",  # chunk-triggered understanding queue; no FK path
    "understanding_chunk_calls",  # raw record of its provider calls; no FK path
    "understanding_group_state",  # upper-level grouping cursor; no FK path
    "understanding_group_calls",  # raw record of its provider calls; no FK path
    "understanding_builds",  # manual builds (FK to the rebuild queue, so before it)
    "understanding_rebuilds",  # upper-level rebuild queue; no FK path to the tables above
    "user_settings",  # no FK, per-test key/value data (audit round-2 cc-docs-tests P2)
    "web_sessions",
    # The cluster extension registry (issue #39 S2). `extensions` FKs to
    # `extension_blobs`, so truncating the blobs cascades to the rows — but both
    # are listed, because the cascade direction is the opposite of the reading
    # order and a future row without a blob (a `source='repo'` row carries none)
    # would otherwise survive into the next test.
    "extensions",
    "extension_blobs",
    # Plugin stat values (task #2911): no FKs, but a leaked row would render in
    # the next test's dashboard response.
    "plugin_stats",
    # The append-only audit record: a leaked row would show in the next test's
    # whole-table reads (the fleet graph, the neighbors walk).
    "audit_events",
    # The append-only telemetry/log record, same reason as audit_events.
    "telemetry_events",
    # im-bridge durable cursors: no FK path; a leaked row would make the next
    # test's bridge resume (or replay) from a stale position.
    "weixin_ingress_receipts",
    "weixin_ingress_cursors",
    "weixin_ingress_bindings",
    "im_bridge_cursors",
    "im_bridge_notice_acceptances",
    "im_bridge_notice_poll_state",
    "im_bridge_alert_acceptances",
    "native_graph_work",
    "native_restart_commands",
    "native_cancel_commands",
    "native_compact_commands",
    "native_compact_observations",
    "im_bridge_outbound_intents",
    "im_bridge_outbound_replays",
    # ttl-reaper cadence clocks: no FK path; a leaked stamp would make the next
    # test's slow phase read as not due.
    "maintenance_state",
    # Queued schedule-manager sync requests: no FK (a delete queues one for a
    # row that is gone); a leaked row would be consumed by the next test.
    "schedule_sync_requests",
    "schedule_operation_receipts",
    "task_patch_receipts",
    "task_update_receipts",
    "notice_operation_receipts",
    "task_creation_receipts",
    "task_assignment_receipts",
    "agent_control_receipts",
    "agent_upload_batches",
    "upload_delivery_batches",
    "upload_delivery_copies",
    "agent_launch_retry_receipts",
    "agent_creation_snapshots",
    "resource_creation_receipts",
    "page_operation_receipts",
)


@pytest.fixture(autouse=True)
def _default_gateway_credentials(
    monkeypatch: pytest.MonkeyPatch, pytestconfig: pytest.Config
) -> None:
    """Give in-process clients of an assembled Gateway app its configured cluster secret as bearer.

    The cluster API always authenticates, so the suite keeps its cluster secret
    (`env_bootstrap`) and every `TestClient` over an assembled Gateway app carries its
    `app.state.gateway_config.cluster_secret` as bearer by default
    (`gateway.tests.agents.gateway_auth`); authentication tests configure their app or
    drop the header (`without_credentials`). The patch touches no store,
    so it applies to every test outside the static environment.
    """
    if not static_mode(pytestconfig):
        install_default_gateway_credentials(monkeypatch)


@pytest.fixture
def _db_stall_guard(provisioned_db: str, postgres_access: None) -> Iterator[None]:
    """Own the cluster probe until this function's admitted DB users finish."""
    from psycopg.conninfo import conninfo_to_dict

    from base.cluster.dataplane.pg_stall_watchdog import StallWatchdogConfig, stall_guard

    port = conninfo_to_dict(provisioned_db)["port"]
    assert isinstance(port, str), "the native test database must publish its port"
    with stall_guard(provisioned_db, int(port), config=StallWatchdogConfig()):
        yield


@pytest.fixture
def _clean_db(
    provisioned_db: str,
    postgres_access: None,
    _db_stall_guard: None,
    monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
) -> Iterator[None]:
    """Drain prior writes and truncate before each admitted database consumer."""
    import psycopg

    from base.config import settings
    from base.telemetry import event_store
    from base.telemetry.emitter import sync
    from base.telemetry.metrics.observed_metrics import close_projection

    monkeypatch.setattr(settings.data_plane, "db_url", provisioned_db)
    monkeypatch.setitem(os.environ, "AVA_DB_URL", provisioned_db)
    env_file = TEST_AVA_HOME / ".env"
    previous_file = env_file.read_text()
    request.addfinalizer(functools.partial(env_file.write_text, previous_file))
    _rewrite_test_env_file_line(env_file, "AVA_DB_URL", provisioned_db)
    event_store.set_enabled(enabled=False)
    # Barrier before the TRUNCATE: the telemetry drain thread may hold a batch
    # dequeued during the previous test (events are written by that single
    # thread up to one flush_interval after enqueue). sync() drains the queue
    # AND waits for the held batch to land, so a straggler batch cannot land
    # after this truncate (the test_events_api / test_agent_events_query
    # exact-content flake class). Cheap: a marker
    # round-trip on an idle drain thread. No-op when the pipeline is absent or
    # already stopped.
    sync()
    close_projection()
    event_store.close_store()
    with psycopg.connect(settings.data_plane.db_url) as conn:
        # Retried on DeadlockDetected: the tolerated straggler class documented
        # on the TRUNCATE below (a background writer leaked from a prior test on
        # this worker) can also DEADLOCK this TRUNCATE, not just write to a dead
        # id. The
        # straggler's snapshot query (`base/agents/observation/snapshot.py`: agents_meta
        # LEFT JOIN inbound_messages) takes its relation locks in the opposite
        # order to this TRUNCATE list (inbound_messages first, agents_meta
        # second) — and other product transactions (deliver_chat_inbound) lock in
        # the TRUNCATE's order, so no single list order dodges every straggler
        # shape. Postgres aborts one side after deadlock_timeout; if it picks us,
        # the straggler has by then finished or died, so one retry wins.
        for attempt in range(3):
            try:
                with conn.cursor() as cur:
                    # TRUNCATE without RESTART IDENTITY: serial ids grow monotonically for
                    # the whole worker-session and are never reused across tests. Reuse
                    # (every test's first spawn = id 1) was the amplifier that turned a
                    # background writer leaked from a prior test into a live-row corruption
                    # of the next test's reused id (the CI-only lifecycle-state collision
                    # flake class). Monotonic ids make a straggler write land on a dead id
                    # instead. Tests that need a stable self-identity take `self_agent`.
                    # audit_events and telemetry_events refuse TRUNCATE through their append-only triggers by
                    # design; the harness resets it with triggers off for this
                    # transaction only (a test database owner, never production).
                    cur.execute("SET LOCAL session_replication_role = replica")
                    cur.execute("TRUNCATE " + ", ".join(_PER_TEST_TRUNCATE_TABLES) + " CASCADE")
                conn.commit()
                break
            except psycopg.errors.DeadlockDetected:
                if attempt == 2:
                    raise
                conn.rollback()
    try:
        yield
    finally:
        sync()
        close_projection()
        event_store.close_store()
        event_store.set_enabled(enabled=False)


@pytest.fixture
def _clean_redis(
    provisioned_redis: str,
    redis_access: None,
    monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
) -> Iterator[None]:
    """Flush only the server admitted by this consumer's Redis dependency."""
    import redis

    from base.config import settings

    monkeypatch.setattr(settings.data_plane, "redis_url", provisioned_redis)
    monkeypatch.setitem(os.environ, "AVA_REDIS_URL", provisioned_redis)
    env_file = TEST_AVA_HOME / ".env"
    previous_file = env_file.read_text()
    request.addfinalizer(functools.partial(env_file.write_text, previous_file))
    _rewrite_test_env_file_line(env_file, "AVA_REDIS_URL", provisioned_redis)
    with redis.Redis.from_url(provisioned_redis) as client:
        client.flushdb()
    yield


def _fail_on_leaked_os_jobs(session: pytest.Session) -> None:
    """Fail the run when it armed or rewrote a job in the host's OS scheduler.

    The counterpart to OS_JOBS_ENABLED, the default-home gate and the helper
    native-effect guard: these keep ordinary unit tests out of the native
    scheduler, and this proves it. A new or changed job here means some path
    reached the scheduler anyway — a subprocess that lost the env, or a registrar
    added without the gate — and, since a label names a job and not a home, it
    replaced the host's real one, which is why this is a hard failure and not a
    warning.

    Nothing is removed: the job found is the host's own, rewritten. It is also
    possible that it is an `ava start` the operator ran in another terminal, so
    check before repairing it.
    """
    leaked = sorted(host_ava_os_jobs() - _OS_JOBS_AT_START)
    if not leaked:
        return
    print(  # noqa: T201 — must reach the terminal; loguru output is captured
        "\nOS-SCHEDULER LEAK: this pytest run registered or rewrote "
        f"{len(leaked)} job(s) on the host:\n  "
        + "\n  ".join(leaked)
        + "\nThe suite runs with AVA_OS_JOBS_ENABLED=false (see "
        "base.host.system.cron.os_jobs_enabled), only the default home may register "
        "a job (base.host.system.cron.owns_os_jobs), and the helper native-effect "
        "guard is on. A job listed here means a test bypassed one of these "
        "boundaries. The job was left in place: it replaced the host's own, so "
        "re-run `ava converge` to restore it, unless this was your own `ava start`.",
        file=sys.stderr,
    )
    session.exitstatus = 1


# Peak memory above which a pytest session has stopped being a test run.
#
# A healthy serial `tests/cli` peaks around 0.2 GB, so this is ~30x headroom and
# cannot flake on a legitimately heavy test — it exists to catch the runaway
# class, where a bounded product loop loses its throttle and allocates until the
# host gives out. On 2026-07-30 that reached 26 GB on a 16 GB box, exhausted swap
# and degraded every process on it, including the prod cluster (issue #1001).
# Nothing failed; the run just got slow, and the incident was diagnosed from the
# outside. This turns the next one into a red test.
_PEAK_MEMORY_CEILING_MB = 6144


def _peak_memory_mb() -> tuple[float, str]:
    """This process's peak memory for the session, as (MB, the gauge's name).

    macOS compresses cold anonymous pages and RSS does not count them, so resident
    size understates a runaway by roughly an order of magnitude (~2 GB resident at
    21 GB footprint during the incident). `footprint(1)`'s `phys_footprint_peak` is
    the gauge that sees all of it; one subprocess, once, at session end. Linux has
    no compressor in the way, so peak RSS there is the whole story.

    """
    import resource

    if sys.platform == "darwin":
        out = subprocess.run(  # noqa: S603 — argv is the static footprint path + our own pid
            ["/usr/bin/footprint", "-p", str(os.getpid())],
            capture_output=True,
            text=True,
            check=False,
        ).stdout
        units = {"B": 1e-6, "KB": 1e-3, "MB": 1.0, "GB": 1e3, "TB": 1e6}
        match = re.search(r"phys_footprint_peak:\s+([\d.]+)\s*(B|KB|MB|GB|TB)", out)
        if match:
            return float(match.group(1)) * units[match.group(2)], "phys_footprint_peak"
        # footprint(1) missing or its output reshaped: fall through to ru_maxrss,
        # which on macOS is BYTES (it is kilobytes on Linux).
        return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1e6, "ru_maxrss"
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1e3, "ru_maxrss"


def _fail_on_runaway_memory(session: pytest.Session) -> None:
    """Fail the run when this worker's peak memory says something ran away.

    RUSAGE_SELF / this pid only: the throwaway pg and redis are children with their
    own budgets, and what this guards is in-process growth."""
    peak_mb, gauge = _peak_memory_mb()
    if peak_mb <= _PEAK_MEMORY_CEILING_MB:
        return
    print(  # noqa: T201 — must reach the terminal; loguru output is captured
        f"\nMEMORY CEILING: this pytest worker peaked at {peak_mb / 1024:.1f} GiB "
        f"({gauge}), over the {_PEAK_MEMORY_CEILING_MB / 1024:.0f} GiB ceiling in "
        "tests/fixtures/provisioning.py. A test suite does not need that much memory, so this is "
        "a runaway rather than a big test: look for a product retry loop bounded by "
        "wall clock whose sleep a test replaced with a no-op (issue #1001), or a "
        "recorder list a stubbed call appends to without bound. Profile with "
        "`footprint -p <pid>` per test rather than ps RSS, which under-reports on "
        "macOS by roughly 10x.",
        file=sys.stderr,
    )
    session.exitstatus = 1


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    """Session end: fail on leaked OS-scheduler jobs and on a runaway memory peak,
    then clean the tmpfs AVA_HOME. The Postgres / Redis clusters are torn down by
    the `provisioned_db` / `provisioned_redis` fixture finalizers (the cluster
    process + data dir are removed), so there is no database to drop here."""
    import shutil

    _fail_on_leaked_os_jobs(session)
    _fail_on_runaway_memory(session)
    shutil.rmtree(TEST_AVA_HOME, ignore_errors=True)


@pytest.fixture
def gateway_data_plane(
    db_url: str,
    redis_url: str,
    guard_schedule_session_control: None,
) -> None:
    """An assembled in-process Gateway consumes both resources and its owner guards."""


@pytest.fixture
def cluster_defaults_unset(db_conn) -> Iterator[None]:
    """Start the test from "the cluster has made no default-model choice".

    `cluster_defaults` is a seeded singleton (like cluster_pin) and is deliberately
    NOT in the per-test TRUNCATE list, and the session DB has the default-model
    migration applied — so without this a birth-config test would silently read the
    migration's seeded value. Snapshot / NULL / restore, so tests that assert the
    fall-through and tests that assert the row both start from a known state."""
    with db_conn.cursor() as cur:
        cur.execute("SELECT llm_model FROM cluster_defaults WHERE id = 1")
        row = cur.fetchone()
        saved = row[0] if row else None
        cur.execute("UPDATE cluster_defaults SET llm_model = NULL WHERE id = 1")
    db_conn.commit()
    try:
        yield
    finally:
        with db_conn.cursor() as cur:
            cur.execute("UPDATE cluster_defaults SET llm_model = %s WHERE id = 1", (saved,))
        db_conn.commit()


@pytest.fixture
def db_url(provisioned_db: str, _clean_db: None) -> str:
    """The session's test database URL, for tests that open their own connections."""
    return provisioned_db


@pytest.fixture(scope="session")
def database_version(pytestconfig: pytest.Config) -> CodeVersion:
    """Resolve the session entry image before test subprocess doubles are installed."""
    return CodeVersion(pytestconfig.stash[_PROCESS_IMAGE_KEY])


@pytest.fixture
def database_gate(database_version: CodeVersion):
    """One test's nonexempt admission budget, shared by its explicit database builders."""
    from base.db.code_version_gate import ProcessDbGate
    from base.telemetry import process_name

    return ProcessDbGate(process=process_name(), version=database_version.get)


@pytest.fixture
def database(db_url: str, database_gate):
    """The native test handle, sharing its test's explicit admission owner."""
    from base.cluster.dataplane.direct_url import ClusterDirectUrl
    from base.db import Database

    # The test cluster is loopback, so the direct (admin-plane) dials tests make need no host reader.
    return Database.from_settings(
        gate=database_gate,
        direct_url=ClusterDirectUrl(lambda: "127.0.0.1", gateway_url_reader=lambda: ""),
    )


@pytest.fixture
def publish_wake(database, event_bus) -> Callable[[int, str], bool]:
    """`publish_inbound_wake` bound to the session's handles, the wake callable a delivery takes."""
    from base.agents.impersonation.wake import publish_inbound_wake

    return functools.partial(publish_inbound_wake, database, event_bus)


@pytest.fixture
def insert_inbound(database, event_bus) -> Callable[..., int]:
    """`insert_inbound_message` bound to the session's handles."""
    from base.agents.inbox.insert import insert_inbound_message

    return functools.partial(insert_inbound_message, database=database, bus=event_bus)


@pytest.fixture
def redis_url(provisioned_redis: str, _clean_redis: None) -> str:
    """The admitted per-test Redis URL, including a clean initial keyspace."""
    return provisioned_redis


@pytest.fixture
def event_bus(redis_url: str):
    """The `EventBus` handle a composition root would pass down, bound to the session's test Redis."""
    from base.events.live.bus import EventBus

    return EventBus.from_settings()


@pytest.fixture
def db_conn(db_url: str):
    """A sync psycopg.Connection on the requested test DB. Per-test isolation
    is handled by the `db_url` dependency, so this fixture only opens and
    closes the connection."""
    import psycopg

    conn = psycopg.connect(db_url)
    try:
        yield conn
    finally:
        conn.close()


@pytest_asyncio.fixture
async def adb_conn(db_url: str):
    """async counterpart of db_conn (the kernel's agent/db/__init__.py uses async). Cleanup
    is the `db_url` dependency; this fixture only opens the AsyncConnection."""
    import psycopg

    aconn = await psycopg.AsyncConnection.connect(db_url)
    try:
        yield aconn
    finally:
        await aconn.close()


@pytest_asyncio.fixture
async def aops_pool(db_url: str):
    """AsyncConnectionPool mirroring prod `agent/loop.py`'s db_pool config
    (the handle AvaContext exposes as `ops_pool`; autocommit + check_connection,
    small). Cleanup is the `db_url` dependency."""
    from psycopg_pool import AsyncConnectionPool

    async with AsyncConnectionPool(
        db_url,
        min_size=1,
        max_size=2,
        kwargs={"autocommit": True, "prepare_threshold": None},
        check=AsyncConnectionPool.check_connection,
        open=False,
    ) as pool:
        yield pool


@pytest_asyncio.fixture
async def aredis_inbound_listener(redis_url: str):
    """RedisInboundListener for wait_for_inbound / claim_node tests. Like prod:
    a dedicated Redis pub/sub subscription, self-reconnecting. Bound to the
    fixed pseudo-agent channel (agent_id=0); tests that park in a real wake need
    the listener's channel to match their agent, so they build their own
    per-agent listener instead. Closed on teardown."""
    from base.events.live.redis_listener import RedisInboundListener

    listener = RedisInboundListener(redis_url, agent_id=0)
    try:
        yield listener
    finally:
        await listener.close()
