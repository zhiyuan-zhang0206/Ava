"""`ClientSet`: clients are built on first use, owned by the set, and released with it."""

from __future__ import annotations

import sys
import threading
from pathlib import Path
from typing import Any

import httpx
import pytest

from base.agents.context import MeivaContext
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


def test_explicit_builders_keep_type_keys_and_rebuild_after_close() -> None:
    class ConfiguredClient(_Closable):
        def __init__(self, value: str) -> None:
            super().__init__()
            self.value = value

    inputs = {"first": "one", "second": "two"}
    first = ClientSet(factories={ConfiguredClient: lambda: ConfiguredClient(inputs["first"])})
    second = ClientSet(factories={ConfiguredClient: lambda: ConfiguredClient(inputs["second"])})
    assert not first._made and not second._made
    original = first.get(ConfiguredClient)
    assert original.value == "one"
    assert first.get(ConfiguredClient) is original
    assert second.get(ConfiguredClient).value == "two"
    inputs["first"] = "updated"
    assert first.get(ConfiguredClient) is original
    first.close()
    assert original.closed
    assert first.get(ConfiguredClient) is not original
    assert first.get(ConfiguredClient).value == "updated"
    assert second.get(ConfiguredClient).value == "two"
    first.close()
    second.close()


def test_explicit_builder_failure_is_not_replaced_by_a_default() -> None:
    error = ValueError("invalid supplied configuration")

    def refuses() -> _Closable:
        raise error

    clients = ClientSet(factories={_Closable: refuses})
    with pytest.raises(ValueError) as observed:
        clients.get(_Closable)
    assert observed.value is error
    assert not clients._made


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


def test_close_releases_the_gateway_client_the_set_built_and_the_next_use_rebuilds() -> None:
    clients = ClientSet(
        gateway_url="http://own.test", gateway=lambda url: httpx.Client(base_url=url)
    )
    own = clients.gateway

    clients.close()
    assert own.is_closed
    rebuilt = clients.gateway
    assert rebuilt is not own
    assert not rebuilt.is_closed
    clients.close()
    assert rebuilt.is_closed


def test_a_derived_gateway_never_reaches_the_owner_it_came_from() -> None:
    parent = ClientSet(
        gateway_url="http://own.test", gateway=lambda url: httpx.Client(base_url=url)
    )
    handed = httpx.Client(base_url="http://derived.test")
    derived = parent.with_gateway(lambda _url: handed)
    seen: dict[str, httpx.Client] = {}
    barrier = threading.Barrier(2)

    def read(name: str, clients: ClientSet) -> None:
        barrier.wait(timeout=5)
        seen[name] = clients.gateway

    threads = [
        threading.Thread(target=read, args=(name, clients))
        for name, clients in (("derived", derived), ("parent", parent))
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)

    assert seen["derived"] is handed
    assert seen["parent"] is not handed
    assert seen["parent"].base_url.host == "own.test"
    parent.close()
    assert not handed.is_closed
    handed.close()


def test_closing_a_derived_sibling_releases_the_client_its_builder_returned() -> None:
    parent = ClientSet(
        gateway_url="http://own.test", gateway=lambda url: httpx.Client(base_url=url)
    )
    handed = httpx.Client(base_url="http://derived.test")
    derived = parent.with_gateway(lambda _url: handed)

    assert derived.gateway is handed
    derived.close()
    assert handed.is_closed
    assert not parent.gateway.is_closed
    parent.close()


def test_a_client_that_cannot_be_built_raises_at_the_call() -> None:
    def refuses() -> Any:
        raise OSError("no socket")

    with pytest.raises(OSError, match="no socket"):
        ClientSet().get(refuses)


def test_cold_export_observation_and_finalization_build_nothing() -> None:
    def forbidden() -> object:
        pytest.fail("a cold ClientSet must not construct an exporter for cleanup or diagnostics")

    clients = ClientSet(pipeline_factory=forbidden)
    assert clients.exports_deferred() is None
    clients.finalize_events()
    clients.close()


def test_description_carries_the_gateway_endpoint_and_no_secret() -> None:
    from base.agents.context.identity import AgentIdentity

    host = MeivaContext(
        identity=AgentIdentity(agent_id=7, owns_loop=True),
        clients=ClientSet(gateway_url="http://gateway.test:8000"),
    )
    description = host.describe()
    assert description["gateway_url"] == "http://gateway.test:8000"
    assert "secret" not in repr(description).lower()

    from base.cluster.machine import MachineIdentity

    machine = MachineIdentity(
        name=lambda: "child",
        role=lambda: frozenset({"agent-runner"}),
        host=lambda: "localhost",
        description=lambda: None,
    )
    child = MeivaContext.from_description(
        description, clients=ClientSet(gateway_url=description["gateway_url"]), machine=machine
    )
    assert child.require_machine() is machine
    assert "machine" not in description
    assert child.identity == host.identity
    assert child.clients.gateway_url == "http://gateway.test:8000"


def test_building_a_context_imports_no_connection_stack() -> None:
    """The exec child builds an `MeivaContext` at boot; the clients wait for first use."""
    import subprocess

    proc = subprocess.run(  # noqa: S603 — fixed argv, sys.executable is trusted
        [sys.executable, "-I", str(Path(__file__).with_name("_context_import_child.py"))],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip().endswith("[]")


def test_a_built_event_pipeline_is_returned_while_another_thread_builds_a_client() -> None:
    """The log sink's producer must not queue behind a client build that waits on config.

    A thread logging from inside the eager config build is the one the SDK thread's gateway
    build waits for; if the producer needed the set lock it held, both would wait on each other.
    """
    build_started = threading.Event()
    release_build = threading.Event()
    pipeline = object()

    def slow_gateway(url: str) -> httpx.Client:
        build_started.set()
        assert release_build.wait(timeout=30)
        return httpx.Client(base_url=url)

    clients = ClientSet(
        gateway_url="http://gateway.test", gateway=slow_gateway, pipeline_factory=lambda: pipeline
    )
    assert clients.event_pipeline() is pipeline
    builder = threading.Thread(target=lambda: clients.gateway)
    builder.start()
    assert build_started.wait(timeout=10)
    returned: list[Any] = []
    reader = threading.Thread(target=lambda: returned.append(clients.event_pipeline()))
    reader.start()
    reader.join(timeout=5)
    blocked = reader.is_alive()
    release_build.set()
    builder.join(timeout=10)
    reader.join(timeout=10)

    assert not blocked, "event_pipeline() waited for an unrelated client build"
    assert returned == [pipeline]
