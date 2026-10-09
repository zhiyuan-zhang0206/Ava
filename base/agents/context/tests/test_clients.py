"""`ClientSet`: clients are built on first use, owned by the set, and released with it."""

from __future__ import annotations

import sys
from typing import Any

import httpx
import pytest

from base.agents.context import AvaContext
from base.agents.context.clients import ClientSet


class _Closable:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


def test_nothing_is_built_until_first_use() -> None:
    clients = ClientSet(
        gateway_url="http://gateway.test", gateway=lambda url: httpx.Client(base_url=url)
    )

    assert clients._gateway is None
    assert "not connected" in repr(clients.sql)
    assert "not connected" in repr(clients.redis)
    assert clients.sql is clients.sql  # the proxy is stable; the connection is built on use
    assert clients.gateway.base_url == "http://gateway.test"
    assert clients.gateway is clients.gateway


def test_get_builds_once_per_factory_and_close_releases_what_it_built() -> None:
    clients = ClientSet()
    built: list[_Closable] = []

    def factory() -> _Closable:
        built.append(_Closable())
        return built[-1]

    assert clients.get(factory) is clients.get(factory)
    assert len(built) == 1
    clients.close()
    assert built[0].closed
    assert clients.get(factory) is built[1]  # a closed set builds afresh


def test_close_closes_the_connections_it_made_and_keeps_going_past_a_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clients = ClientSet()
    sql, redis = _Closable(), _Closable()
    monkeypatch.setattr(clients, "_connect_sql", lambda: sql)
    monkeypatch.setattr(clients, "_connect_redis", lambda: redis)
    assert clients.sql._get() is sql
    assert clients.redis._get() is redis

    def explode() -> None:
        raise RuntimeError("close failed")

    sql.close = explode  # type: ignore[method-assign]
    clients.close()

    assert redis.closed  # closed despite the SQL close failing


def test_using_gateway_routes_and_restores_without_closing_the_provided_client() -> None:
    clients = ClientSet(
        gateway_url="http://own.test", gateway=lambda url: httpx.Client(base_url=url)
    )
    own = clients.gateway
    provided = httpx.Client(base_url="http://provided.test")

    with clients.using_gateway(provided) as active:
        assert active is provided
        assert clients.gateway is provided
    assert clients.gateway is own

    clients.close()
    assert own.is_closed
    assert not provided.is_closed  # never the set's to close
    provided.close()


def test_a_client_that_cannot_be_built_raises_at_the_call() -> None:
    def refuses() -> Any:
        raise OSError("no socket")

    with pytest.raises(OSError, match="no socket"):
        ClientSet().get(refuses)


def test_description_carries_the_gateway_endpoint_and_no_secret() -> None:
    from base.agents.context.identity import AgentIdentity

    host = AvaContext(
        identity=AgentIdentity(agent_id=7, owns_loop=True),
        clients=ClientSet(gateway_url="http://gateway.test:8000"),
    )
    description = host.describe()
    assert description["gateway_url"] == "http://gateway.test:8000"
    assert "secret" not in repr(description).lower()

    child = AvaContext.from_description(
        description, clients=ClientSet(gateway_url=description["gateway_url"])
    )
    assert child.identity == host.identity
    assert child.clients.gateway_url == "http://gateway.test:8000"


def test_building_a_context_imports_no_connection_stack() -> None:
    """The exec child builds an `AvaContext` at boot; the clients wait for first use."""
    import subprocess

    code = (
        "import sys\n"
        "from base.agents.context import AvaContext\n"
        "from base.agents.context.identity import AgentIdentity\n"
        "AvaContext(identity=AgentIdentity(1, True)).describe\n"
        "print(sorted(m for m in ('psycopg_pool', 'redis', 'httpx', 'langchain_core') "
        "if m in sys.modules))\n"
    )
    proc = subprocess.run(  # noqa: S603 — fixed argv, sys.executable is trusted
        [sys.executable, "-I", "-c", code], capture_output=True, text=True, timeout=120, check=False
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip().endswith("[]")


def test_custom_database_dials_its_own_resource_with_no_process_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from base.config import settings
    from base.db import Database

    database = Database.from_settings()
    clients = ClientSet(database=lambda: database)
    monkeypatch.setattr(settings.data_plane, "db_url", "")
    try:
        assert clients.sql.execute("SELECT 42").fetchone() == (42,)
    finally:
        clients.close()
