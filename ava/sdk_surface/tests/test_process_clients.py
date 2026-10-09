"""Process composition supplies lazy, child-local resources to ClientSet."""

import sys

import psycopg
import pytest
import redis

from ava.sdk_surface.process_context import context_from_description, process_clients
from base.agents.context import AvaContext
from base.agents.context.clients import ClientSet
from base.agents.context.identity import AgentIdentity
from base.config import settings
from base.db import Database


def test_default_sql_refuses_a_missing_resource_before_dial(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clients = process_clients()
    monkeypatch.setattr(settings.data_plane, "db_url", "")

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
) -> None:
    database = Database.from_settings()
    custom = process_clients(database=lambda: database)
    default = process_clients()
    monkeypatch.setattr(settings.data_plane, "db_url", "")
    try:
        assert custom.sql.execute("SELECT 44").fetchone() == (44,)
        with pytest.raises(RuntimeError, match="AVA_DB_URL not set"):
            default.sql.execute("SELECT 1")
    finally:
        custom.close()
        default.close()


def test_process_resources_resolve_after_overlay_and_again_after_close(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_url, redis_url = settings.data_plane.db_url, settings.data_plane.redis_url
    monkeypatch.setattr(settings.data_plane, "db_url", "")
    monkeypatch.setattr(settings.data_plane, "redis_url", "")
    clients = process_clients()
    monkeypatch.setattr(settings.data_plane, "db_url", db_url)
    monkeypatch.setattr(settings.data_plane, "redis_url", redis_url)
    try:
        assert clients.sql.execute("SELECT 43").fetchone() == (43,)
        assert clients.redis.set("process-clients-overlay", "ready")
        assert clients.redis.get("process-clients-overlay") == "ready"
        native = clients.redis._get()
        assert type(native) is redis.Redis
        assert native.connection_pool.connection_kwargs["socket_timeout"] == 10.0
        clients.close()
        monkeypatch.setattr(settings.data_plane, "db_url", "")
        monkeypatch.setattr(settings.data_plane, "redis_url", "")
        with pytest.raises(RuntimeError, match="AVA_DB_URL not set"):
            clients.sql.execute("SELECT 1")
        with pytest.raises(RuntimeError, match="AVA_REDIS_URL not set"):
            clients.redis.ping()
    finally:
        clients.close()


def test_child_uses_description_endpoint_and_credentials_at_first_use(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    host = AvaContext(
        identity=AgentIdentity(17, True),
        clients=ClientSet(gateway_url="http://127.0.0.1:9"),
    )
    description = host.describe()
    assert set(description) == {"identity", "gateway_url"}
    monkeypatch.setenv("AVA_PROCESS_PROFILE", "agent")
    monkeypatch.setenv("AVA_API_TOKEN", "before-overlay")
    child = context_from_description(description)
    monkeypatch.setenv("AVA_API_TOKEN", "after-overlay")
    monkeypatch.setattr(settings.gateway, "gateway_client_http_timeout_seconds", 17.0)
    monkeypatch.setattr(settings.gateway, "gateway_url", "http://unrelated.test")
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
        "import sys\n"
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
