# meiva:note
# Native fixture cleanup covers every public per-test table, uses a private home and test-only URLs, and keeps the full-run selection guard active.
# meiva:link tests/fixtures/docs/native-provisioning.meiva.okf.md
from __future__ import annotations

import functools
import os
import re
import sys
from collections.abc import Callable, Iterator, Mapping
from pathlib import Path

import pytest
import pytest_asyncio

from base.cluster.dataplane.pg_throwaway_base import ThrowawayConfig, configured_base
from base.native_process.code_version import CodeVersion
from base.native_process.loaded_commit import LoadedCommit
from gateway.tests.agents.gateway_auth import install_default_gateway_credentials
from tests._os_jobs import host_meiva_os_jobs
from tests._test_env_file import rewrite_line as _rewrite_test_env_file_line
from tests.fixtures.env_bootstrap import (
    TEST_MEIVA_HOME,
)
from tests.fixtures.static_environment import static_mode

_OS_JOBS_AT_START = host_meiva_os_jobs()
_PROCESS_IMAGE_KEY = pytest.StashKey[LoadedCommit]()


@pytest.fixture(scope="session")
def throwaway_config(process_config) -> ThrowawayConfig:
    return ThrowawayConfig(configured_base(process_config.view.data_plane.pg_throwaway_base))


@pytest.fixture(scope="session")
def provisioned_db(
    pytestconfig: pytest.Config,
    process_config,
    database_version: CodeVersion,
    throwaway_config: ThrowawayConfig,
) -> Iterator[str]:
    if static_mode(pytestconfig):
        pytest.fail("Static tests cannot request Postgres; use the native test environment.")
    from langgraph.checkpoint.postgres import PostgresSaver

    from base.cluster.machine.boot import machine_from_boot
    from base.db.test_db_guard import assert_test_db_url
    from base.deploy.tests.containers import postgres

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
    if static_mode(pytestconfig):
        pytest.fail("Static tests cannot request Redis; use the native test environment.")
    from base.deploy.tests.containers import redis_server

    with redis_server() as url:
        process_config.set_field("redis_url", url)
        yield url


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
    if env.get("MEIVA_ALLOW_FULL_PYTEST") == "1":
        return None

    shared_roots = {
        (Path.home() / "Meiva").resolve(),
        (Path.home() / ".meiva" / "source").resolve(),
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

        if selected_path.is_absolute():
            candidates = [selected_path]
        else:
            candidates = [cwd / selected_path, resolved_rootpath / selected_path]
        if any(candidate.is_file() for candidate in candidates):
            return None

    return """\
Full local pytest runs are blocked on this shared Meiva checkout.
User ruling (2026-08-24): do not run the full suite on the shared production host.
Run a targeted test instead:
  pytest tests/<path>::<func>
Directory-only selections are blocked because they still collect too much.
Full-suite verification runs in GitHub CI when you push.
For an explicitly user-approved local full run, use:
  MEIVA_ALLOW_FULL_PYTEST=1 pytest ...
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
    from tests.fixtures.env_bootstrap import UNPROVISIONED_DB_URL

    assert os.environ["MEIVA_DB_URL"] == UNPROVISIONED_DB_URL


_PER_TEST_TRUNCATE_TABLES = (
    "inbound_messages",
    "agents_meta",
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
    "mcp_clients",
    "mcp_credential_creation_receipts",
    "api_idempotency",
    "checkpoint_blobs",
    "checkpoint_writes",
    "checkpoints",
    "understanding_nodes",
    "run_timeline_segments",
    "run_timeline_message_facts",
    "run_timeline_blocks",
    "understanding_chunk_jobs",
    "understanding_chunk_calls",
    "understanding_group_state",
    "understanding_group_calls",
    "understanding_builds",
    "understanding_rebuilds",
    "user_settings",
    "web_sessions",
    "extensions",
    "extension_blobs",
    "plugin_stats",
    "audit_events",
    "telemetry_events",
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
    "maintenance_state",
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
    if not static_mode(pytestconfig):
        install_default_gateway_credentials(monkeypatch)


@pytest.fixture
def _db_stall_guard(provisioned_db: str, postgres_access: None) -> Iterator[None]:
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
    import psycopg

    from base.config import settings
    from base.telemetry import event_store
    from base.telemetry.emitter import sync
    from base.telemetry.metrics.observed_metrics import close_projection

    monkeypatch.setattr(settings.data_plane, "db_url", provisioned_db)
    monkeypatch.setitem(os.environ, "MEIVA_DB_URL", provisioned_db)
    env_file = TEST_MEIVA_HOME / ".env"
    previous_file = env_file.read_text()
    request.addfinalizer(functools.partial(env_file.write_text, previous_file))
    _rewrite_test_env_file_line(env_file, "MEIVA_DB_URL", provisioned_db)
    event_store.set_enabled(enabled=False)

    sync()
    close_projection()
    event_store.close_store()
    with psycopg.connect(settings.data_plane.db_url) as conn:
        for attempt in range(3):
            try:
                with conn.cursor() as cur:
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
    import redis

    from base.config import settings

    monkeypatch.setattr(settings.data_plane, "redis_url", provisioned_redis)
    monkeypatch.setitem(os.environ, "MEIVA_REDIS_URL", provisioned_redis)
    env_file = TEST_MEIVA_HOME / ".env"
    previous_file = env_file.read_text()
    request.addfinalizer(functools.partial(env_file.write_text, previous_file))
    _rewrite_test_env_file_line(env_file, "MEIVA_REDIS_URL", provisioned_redis)
    with redis.Redis.from_url(provisioned_redis) as client:
        client.flushdb()
    yield


def _fail_on_leaked_os_jobs(session: pytest.Session) -> None:
    leaked = sorted(host_meiva_os_jobs() - _OS_JOBS_AT_START)
    if not leaked:
        return
    sys.stderr.write(
        "\nOS-SCHEDULER LEAK: this pytest run registered or rewrote "
        f"{len(leaked)} job(s) on the host:\n  "
        + "\n  ".join(leaked)
        + "\nThe suite runs with MEIVA_OS_JOBS_ENABLED=false (see "
        "base.host.system.cron.os_jobs_enabled), only the default home may register "
        "a job (base.host.system.cron.owns_os_jobs), and the helper native-effect "
        "guard is on. A job listed here means a test bypassed one of these "
        "boundaries. The job was left in place: it replaced the host's own, so "
        "re-run `meiva converge` to restore it, unless this was your own `meiva start`.\n",
    )
    session.exitstatus = 1


_PEAK_MEMORY_CEILING_MB = 6144


def _peak_memory_mb() -> tuple[float, str]:
    import resource

    from base.host.proc import run_bounded

    if sys.platform == "darwin":
        out = run_bounded(
            ["/usr/bin/footprint", "-p", str(os.getpid())],
            capture_output=True,
            text=True,
            timeout=10,
        ).stdout
        units = {"B": 1e-6, "KB": 1e-3, "MB": 1.0, "GB": 1e3, "TB": 1e6}
        match = re.search(r"phys_footprint_peak:\s+([\d.]+)\s*(B|KB|MB|GB|TB)", out)
        if match:
            return float(match.group(1)) * units[match.group(2)], "phys_footprint_peak"

        return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1e6, "ru_maxrss"
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1e3, "ru_maxrss"


def _fail_on_runaway_memory(session: pytest.Session) -> None:
    peak_mb, gauge = _peak_memory_mb()
    if peak_mb <= _PEAK_MEMORY_CEILING_MB:
        return
    sys.stderr.write(
        f"\nMEMORY CEILING: this pytest worker peaked at {peak_mb / 1024:.1f} GiB "
        f"({gauge}), over the {_PEAK_MEMORY_CEILING_MB / 1024:.0f} GiB ceiling in "
        "tests/fixtures/provisioning.py. A test suite does not need that much memory, so this is "
        "a runaway rather than a big test: look for a product retry loop bounded by "
        "wall clock whose sleep a test replaced with a no-op (issue #1001), or a "
        "recorder list a stubbed call appends to without bound. Profile with "
        "`footprint -p <pid>` per test rather than ps RSS, which under-reports on "
        "macOS by roughly 10x.\n",
    )
    session.exitstatus = 1


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    import shutil

    _fail_on_leaked_os_jobs(session)
    _fail_on_runaway_memory(session)
    shutil.rmtree(TEST_MEIVA_HOME, ignore_errors=True)


@pytest.fixture
def gateway_data_plane(
    db_url: str,
    redis_url: str,
    guard_schedule_session_control: None,
) -> None:
    pass


@pytest.fixture
def cluster_defaults_unset(db_conn) -> Iterator[None]:
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
    return provisioned_db


@pytest.fixture(scope="session")
def database_version(pytestconfig: pytest.Config) -> CodeVersion:
    return CodeVersion(pytestconfig.stash[_PROCESS_IMAGE_KEY])


@pytest.fixture
def database_gate(database_version: CodeVersion):
    from base.db.code_version_gate import ProcessDbGate
    from base.telemetry import process_name

    return ProcessDbGate(process=process_name(), version=database_version.get)


@pytest.fixture
def database(db_url: str, database_gate):
    from base.cluster.dataplane.direct_url import ClusterDirectUrl
    from base.db import Database

    return Database.from_settings(
        gate=database_gate,
        direct_url=ClusterDirectUrl(lambda: "127.0.0.1"),
    )


@pytest.fixture
def publish_wake(database, event_bus) -> Callable[[int, str], bool]:
    from base.agents.impersonation.wake import publish_inbound_wake

    return functools.partial(publish_inbound_wake, database, event_bus)


@pytest.fixture
def insert_inbound(database, event_bus) -> Callable[..., int]:
    from base.agents.inbox.insert import insert_inbound_message

    return functools.partial(insert_inbound_message, database=database, bus=event_bus)


@pytest.fixture
def redis_url(provisioned_redis: str, _clean_redis: None) -> str:
    return provisioned_redis


@pytest.fixture
def event_bus(redis_url: str):
    from base.events.live.bus import EventBus

    return EventBus.from_settings()


@pytest.fixture
def db_conn(db_url: str):
    import psycopg

    conn = psycopg.connect(db_url)
    try:
        yield conn
    finally:
        conn.close()


@pytest_asyncio.fixture
async def adb_conn(db_url: str):
    import psycopg

    aconn = await psycopg.AsyncConnection.connect(db_url)
    try:
        yield aconn
    finally:
        await aconn.close()


@pytest_asyncio.fixture
async def aops_pool(db_url: str):
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
    from base.events.live.redis_listener import RedisInboundListener

    listener = RedisInboundListener(redis_url, agent_id=0)
    try:
        yield listener
    finally:
        await listener.close()
