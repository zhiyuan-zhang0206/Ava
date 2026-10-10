"""Process composition supplies lazy, child-local resources to ClientSet."""

import os
import sys
from collections.abc import Iterator
from unittest.mock import patch

import psycopg
import pytest
import redis

from ava.sdk_surface.process_context import context_from_description, process_clients
from base.agents.context import AvaContext
from base.agents.context.clients import ClientSet
from base.agents.context.identity import AgentIdentity
from base.config import ConfigBoot, settings
from base.db import Database


@pytest.fixture
def config_boot() -> Iterator[ConfigBoot]:
    # An independent boot owns delivery environment mutations in this test scope.
    with patch.dict(os.environ):
        owner = ConfigBoot()
        owner.boot()
        yield owner


def test_default_sql_refuses_a_missing_resource_before_dial(
    monkeypatch: pytest.MonkeyPatch,
    config_boot: ConfigBoot,
) -> None:
    clients = process_clients(config=config_boot)
    config_boot.set_field("db_url", "")

    def unexpected_dial(*args: object, **kwargs: object) -> None:
        raise AssertionError("an empty default DSN must never reach libpq")

    monkeypatch.setattr(psycopg, "connect", unexpected_dial)
    try:
        with pytest.raises(RuntimeError, match="AVA_DB_URL not set"):
            clients.sql.execute("SELECT 1")
    finally:
        clients.close()


def test_process_custom_database_is_independent_of_the_default(
    monkeypatch: pytest.MonkeyPatch,
    config_boot: ConfigBoot,
) -> None:
    database = Database.from_settings()
    custom = process_clients(database=lambda: database, config=config_boot)
    default = process_clients(config=config_boot)
    config_boot.set_field("db_url", "")
    try:
        assert custom.sql.execute("SELECT 44").fetchone() == (44,)
        with pytest.raises(RuntimeError, match="AVA_DB_URL not set"):
            default.sql.execute("SELECT 1")
    finally:
        custom.close()
        default.close()


def test_process_resources_resolve_after_overlay_and_again_after_close(
    monkeypatch: pytest.MonkeyPatch,
    config_boot: ConfigBoot,
) -> None:
    db_url = settings.data_plane.db_url
    redis_url = settings.data_plane.redis_url
    config_boot.set_field("db_url", "")
    config_boot.set_field("redis_url", "")
    clients = process_clients(config=config_boot)
    config_boot.set_field("db_url", db_url)
    config_boot.set_field("redis_url", redis_url)
    try:
        assert clients.sql.execute("SELECT 43").fetchone() == (43,)
        assert clients.redis.set("process-clients-overlay", "ready")
        assert clients.redis.get("process-clients-overlay") == "ready"
        native = clients.redis._get()
        assert type(native) is redis.Redis
        assert native.connection_pool.connection_kwargs["socket_timeout"] == 10.0
        clients.close()
        config_boot.set_field("db_url", "")
        config_boot.set_field("redis_url", "")
        with pytest.raises(RuntimeError, match="AVA_DB_URL not set"):
            clients.sql.execute("SELECT 1")
        with pytest.raises(RuntimeError, match="AVA_REDIS_URL not set"):
            clients.redis.ping()
    finally:
        clients.close()


def test_child_uses_description_endpoint_and_credentials_at_first_use(
    monkeypatch: pytest.MonkeyPatch,
    config_boot: ConfigBoot,
) -> None:
    host = AvaContext(
        identity=AgentIdentity(17, True),
        clients=ClientSet(gateway_url="http://127.0.0.1:9"),
    )
    description = host.describe()
    assert set(description) == {"identity", "gateway_url"}
    monkeypatch.setenv("AVA_PROCESS_PROFILE", "agent")
    monkeypatch.setenv("AVA_API_TOKEN", "before-overlay")
    child = context_from_description(description, config=config_boot)
    monkeypatch.setenv("AVA_API_TOKEN", "after-overlay")
    config_boot.set_field("gateway_client_http_timeout_seconds", 17.0)
    config_boot.set_field("gateway_url", "http://unrelated.test")
    try:
        client = child.gateway
        assert str(client.base_url).rstrip("/") == description["gateway_url"]
        assert client.headers["Authorization"] == "Bearer after-overlay"
        assert client.timeout.read == client.timeout.connect == 17.0
        assert "overlay" not in repr(description)
        child.clients.close()
        assert client.is_closed
        monkeypatch.setenv("AVA_API_TOKEN", "after-close")
        assert child.gateway.headers["Authorization"] == "Bearer after-close"
    finally:
        child.clients.close()


def test_process_boot_builds_no_connection_stack(pytester: pytest.Pytester) -> None:
    code = (
        "import sys, importlib, threading\n"
        "import ava\n"
        "image = ava._LOADED_IMAGE\n"
        "assert 'base.db' not in sys.modules\n"
        "assert not getattr(ava, '__plugin_installation__', None)\n"
        "assert not [t for t in threading.enumerate() if t is not threading.main_thread()]\n"
        "importlib.reload(ava)\n"
        "assert ava._LOADED_IMAGE is image\n"
        "assert 'base.db' not in sys.modules\n"
        "from ava.sdk_surface.process_context import process_clients, context_from_description\n"
        "before = set(sys.modules)\n"
        "process_clients()\n"
        "context_from_description({'identity': {'agent_id': 17, 'owns_loop': True, "
        "'actor': None}, 'gateway_url': 'http://gateway.test'})\n"
        "print(sorted(m for m in ('psycopg', 'psycopg_pool', 'redis', 'httpx', 'langchain_core') "
        "if m in sys.modules and m not in before))\n"
    )
    proc = pytester.run(sys.executable, "-I", "-c", code, timeout=120)
    assert proc.ret == 0, proc.stderr.str()
    proc.stdout.fnmatch_lines(["[]"])


def test_default_clients_capture_only_on_first_read_without_delivery(
    pytester: pytest.Pytester,
) -> None:
    code = """
import os
from ava.gateway_client.transport import GatewayTransportInputs
from ava.sdk_surface.process_context import process_clients
from base.config import ConfigBoot

capture = ConfigBoot.read_process_environment
reads = []
snapshots = []
def observed_capture(owner):
    reads.append(owner.prepared)
    capture(owner)
    snapshots.append(owner.environment)
ConfigBoot.read_process_environment = observed_capture
before = dict(os.environ)
clients = process_clients()
assert reads == []
inputs = clients.get(GatewayTransportInputs)
assert reads == []
assert inputs.max_retries_reader() > 0
assert reads == [False]
assert inputs.retry_delay_reader() >= 0
assert inputs.memory_deadline_reader() > 0
assert reads == [False, True, True]
assert all(snapshot is snapshots[0] for snapshot in snapshots)
assert dict(os.environ) == before
clients.close()
assert clients.get(GatewayTransportInputs).max_retries_reader() > 0
assert snapshots[-1] is snapshots[0]
assert dict(os.environ) == before
clients.close()
"""
    result = pytester.run(sys.executable, "-I", "-c", code, timeout=30)
    assert result.ret == 0, result.stderr.str()


def test_gateway_clients_keep_independent_live_config_and_rebuild_credentials(
    monkeypatch: pytest.MonkeyPatch, config_boot: ConfigBoot
) -> None:
    monkeypatch.delenv("AVA_API_TOKEN", raising=False)
    monkeypatch.setenv("AVA_PROCESS_PROFILE", "gateway")
    second = ConfigBoot()
    config_boot.set_field("gateway_url", "http://first.test/")
    second.set_field("gateway_url", "http://second.test/")
    config_boot.set_field("cluster_secret", "first-secret")
    second.set_field("cluster_secret", "second-secret")
    first_clients = process_clients(config=config_boot)
    second_clients = process_clients(config=second)
    try:
        first_gateway = first_clients.gateway
        second_gateway = second_clients.gateway
        assert str(first_gateway.base_url).rstrip("/") == "http://first.test"
        assert str(second_gateway.base_url).rstrip("/") == "http://second.test"
        assert first_gateway.headers["Authorization"] == "Bearer first-secret"
        assert second_gateway.headers["Authorization"] == "Bearer second-secret"
        first_clients.close()
        config_boot.set_field("cluster_secret", "first-updated")
        assert first_clients.gateway.headers["Authorization"] == "Bearer first-updated"
        assert second_clients.gateway is second_gateway
        assert second_gateway.headers["Authorization"] == "Bearer second-secret"
    finally:
        first_clients.close()
        second_clients.close()
