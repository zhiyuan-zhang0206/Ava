"""`meiva.agents` SDK query/config surface: get_neighbors, get_ancestors, list_agents, spawn config, resurrect, lifecycle enums; split from gateway/tests/agents/test_agents_sdk.py (task #4922)."""

from __future__ import annotations

import uuid
from typing import Any
from uuid import uuid4

import httpx
import psycopg
import pytest

import meiva
from base.cluster.machine import MachineIdentity
from base.cluster.machine.tests.identity import machine_identity as machine_identity
from base.config.service_read import ConfigAuthority
from base.db.code_version_gate import ProcessDbGate
from gateway.tests.agents.sdk_support import (
    sdk_via_gateway as sdk_via_gateway,
)
from gateway.tests.agents.sdk_support import spawn_agent
from meiva import gateway_client
from meiva.agents import AgentNotFound, AgentStatus
from meiva.gateway_client import use_client
from meiva.sdk_surface.installation import Installation
from meiva.tests.fixtures.pin_agent import pin_agent


class TestGetNeighbors:
    """SDK get_neighbors maps the gateway rows to Neighbor dataclasses (status to
    the AgentStatus enum). The graph behaviors are covered in
    gateway/tests/agents/test_agent_neighbors.py; here we verify the wrapper + wire path
    only, seeding ties as `audit_events` rows."""

    @staticmethod
    def _seed(db: psycopg.Connection, *, status: str = "running") -> int:
        with db.cursor() as cur:
            cur.execute("INSERT INTO agents DEFAULT VALUES RETURNING id")
            row = cur.fetchone()
            assert row is not None
            aid = row[0]
            cur.execute(
                "INSERT INTO agents_meta (id, spawner, status) VALUES (%s, 'test', %s)",
                (aid, status),
            )
        db.commit()
        return aid

    @staticmethod
    def _tie(db: psycopg.Connection, agent_id: int, target: int, *, days_ago: float) -> None:
        db.execute(
            "INSERT INTO audit_events (event_uid, ts, machine, process, event_name, level, "
            "source, agent_id, target_agent_id) "
            "VALUES (%s, now() - (%s * interval '1 day'), 'test', 'test', 'send_message', "
            "'info', 'test', %s, %s)",
            (uuid.uuid4().int % (1 << 62), days_ago, agent_id, target),
        )
        db.commit()

    def test_returns_ranked_neighbor_dataclasses(self, db_conn: psycopg.Connection) -> None:
        a = self._seed(db_conn)
        fresh = self._seed(db_conn)
        stale = self._seed(db_conn, status="terminated")
        self._tie(db_conn, fresh, a, days_ago=0.0)
        self._tie(db_conn, stale, a, days_ago=60.0)

        rows = meiva.agents.get_neighbors(a)

        assert all(isinstance(r, meiva.agents.Neighbor) for r in rows)
        assert [r.agent_id for r in rows] == [fresh, stale]  # recent ranks above stale
        assert rows[1].status is AgentStatus.TERMINATED  # terminated neighbor included
        assert rows[0].depth == 1
        assert rows[0].score > rows[1].score
        assert f"#{fresh}" in str(rows[0]) and "depth=1" in str(rows[0])

    def test_defaults_come_from_display_config(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Omitted depth/limit resolve from settings.display.neighbors_default_*
        (``MEIVA_NEIGHBORS_DEFAULT_DEPTH`` / ``MEIVA_NEIGHBORS_DEFAULT_LIMIT``); the
        literals 1/20 are only the fields' defaults, not hard-coded call
        parameters."""
        from base.config import settings

        seen: dict[str, int] = {}

        def _record(_agent_id: int, *, depth: int, limit: int) -> list[dict[str, object]]:
            seen["depth"] = depth
            seen["limit"] = limit
            return []

        monkeypatch.setattr(gateway_client, "get_neighbors", _record)
        monkeypatch.setattr(settings.display, "neighbors_default_depth", 3)
        monkeypatch.setattr(settings.display, "neighbors_default_limit", 7)

        assert meiva.agents.get_neighbors(11) == []
        assert seen == {"depth": 3, "limit": 7}

    def test_nonexistent_raises(self, db_conn: psycopg.Connection) -> None:
        with pytest.raises(AgentNotFound):
            meiva.agents.get_neighbors(9999)


class TestGetAncestors:
    """SDK get_ancestors maps the gateway `ancestors` rows to Neighbor
    dataclasses. The chain walk itself is covered in
    gateway/tests/agents/test_agent_neighbors.py; here we verify the wrapper + wire
    path only. Ancestry is the immutable `agents_meta.born_spawner` chain, so
    it is seeded on the row; the recorded spawn event only feeds the tie
    graph."""

    @staticmethod
    def _seed(
        db: psycopg.Connection, *, status: str = "running", born_spawner: str | None = None
    ) -> int:
        with db.cursor() as cur:
            cur.execute("INSERT INTO agents DEFAULT VALUES RETURNING id")
            row = cur.fetchone()
            assert row is not None
            aid = row[0]
            cur.execute(
                "INSERT INTO agents_meta (id, spawner, born_spawner, status) "
                "VALUES (%s, 'test', %s, %s)",
                (aid, born_spawner, status),
            )
        db.commit()
        return aid

    @staticmethod
    def _spawn(db: psycopg.Connection, child: int, parent: int) -> None:
        # Event direction: agent_id = the new agent, target_agent_id = spawner.
        db.execute(
            "INSERT INTO audit_events (event_uid, ts, machine, process, event_name, level, "
            "source, agent_id, target_agent_id) "
            "VALUES (%s, now(), 'test', 'test', 'spawn', 'info', 'test', %s, %s)",
            (uuid.uuid4().int % (1 << 62), child, parent),
        )
        db.commit()

    def test_returns_spawn_chain_as_neighbor_dataclasses(self, db_conn: psycopg.Connection) -> None:
        a = self._seed(db_conn, status="terminated")
        b = self._seed(db_conn, born_spawner=f"agent:{a}")
        self._spawn(db_conn, b, a)

        rows = meiva.agents.get_ancestors(b)

        assert all(isinstance(r, meiva.agents.Neighbor) for r in rows)
        assert [r.agent_id for r in rows] == [a]  # nearest ancestor first
        assert rows[0].depth == 1  # hops UP from the queried agent
        assert rows[0].status is AgentStatus.TERMINATED  # terminated parent included
        assert f"#{a}" in str(rows[0]) and "depth=1" in str(rows[0])

    def test_no_spawner_returns_empty(self, db_conn: psycopg.Connection) -> None:
        a = self._seed(db_conn)

        assert meiva.agents.get_ancestors(a) == []

    def test_nonexistent_raises(self, db_conn: psycopg.Connection) -> None:
        with pytest.raises(AgentNotFound):
            meiva.agents.get_ancestors(9999)


class TestListAgents:
    def test_gateway_client_reads_exactly_one_page(self) -> None:
        calls: list[httpx.Request] = []
        page: dict[str, object] = {"agents": [], "next_cursor": 17}

        def answer(request: httpx.Request) -> httpx.Response:
            calls.append(request)
            return httpx.Response(200, json=page)

        with (
            httpx.Client(base_url="http://gateway", transport=httpx.MockTransport(answer)) as http,
            use_client(http),
        ):
            assert (
                gateway_client.list_agents(
                    scope="terminated", query="research", before_id=42, limit=5
                )
                == page
            )
        [request] = calls
        assert (request.method, request.url.path) == ("GET", "/api/agents")
        assert dict(request.url.params) == {
            "scope": "terminated",
            "query": "research",
            "before_id": "42",
            "limit": "5",
        }

    def test_default_scope_includes_all_nonterminated_states(
        self,
        db_conn: psycopg.Connection,
        *,
        config_authority: ConfigAuthority,
        database_gate: ProcessDbGate,
        machine_identity: MachineIdentity,
    ) -> None:
        ids = [
            spawn_agent(
                config_authority=config_authority,
                database_gate=database_gate,
                local_machine=machine_identity,
            )
            for _ in range(3)
        ]
        for agent_id, status in zip(ids, ("running", "idling", "terminated"), strict=True):
            db_conn.execute("UPDATE agents_meta SET status = %s WHERE id = %s", (status, agent_id))
        db_conn.commit()

        page = meiva.agents.list_agents()
        assert isinstance(page, meiva.agents.AgentDirectoryPage)
        assert [row.agent_id for row in page.agents] == list(reversed(ids[:2]))
        assert page.next_cursor is None

    def test_terminated_pages_preserve_cursor_and_search(
        self,
        db_conn: psycopg.Connection,
        *,
        config_authority: ConfigAuthority,
        database_gate: ProcessDbGate,
        machine_identity: MachineIdentity,
    ) -> None:
        ids = [
            spawn_agent(
                config_authority=config_authority,
                database_gate=database_gate,
                local_machine=machine_identity,
            )
            for _ in range(5)
        ]
        for agent_id in ids:
            db_conn.execute(
                "UPDATE agents_meta SET status = 'terminated' WHERE id = %s", (agent_id,)
            )
            db_conn.execute(
                "UPDATE agents SET label = %s WHERE id = %s", ("archived worker", agent_id)
            )
        db_conn.execute("UPDATE agents SET label = 'unrelated' WHERE id = %s", (ids[2],))
        db_conn.commit()

        first = meiva.agents.list_agents(scope="terminated", query="archived", limit=2)
        assert [row.agent_id for row in first.agents] == [ids[4], ids[3]]
        assert first.next_cursor == ids[3]
        second = meiva.agents.list_agents(
            scope="terminated",
            query="archived",
            before_id=first.next_cursor,
            limit=2,
        )
        assert [row.agent_id for row in second.agents] == [ids[1], ids[0]]
        assert second.next_cursor is None

    def test_empty_page_has_no_cursor(self, db_conn: psycopg.Connection) -> None:
        page = meiva.agents.list_agents()
        assert page.agents == []
        assert page.next_cursor is None

    @pytest.mark.parametrize(
        "kwargs",
        [
            {"scope": "active"},
            {"limit": 0},
            {"limit": 201},
            {"before_id": 0},
            {"query": "x" * 201},
        ],
    )
    def test_invalid_page_arguments_fail_before_reading(
        self, kwargs: dict[str, Any], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def forbidden(**_kwargs: Any) -> None:
            pytest.fail("invalid page arguments must not make a request")

        monkeypatch.setattr(gateway_client, "list_agents", forbidden)
        with pytest.raises(ValueError):
            meiva.agents.list_agents(**kwargs)

    def test_get_status_uses_direct_detail_not_directory(self) -> None:
        calls: list[httpx.Request] = []

        def answer(request: httpx.Request) -> httpx.Response:
            calls.append(request)
            return httpx.Response(200, json={"status": "terminated"})

        with (
            httpx.Client(base_url="http://gateway", transport=httpx.MockTransport(answer)) as http,
            use_client(http),
        ):
            assert meiva.agents.get_status(1) == AgentStatus.TERMINATED
        [request] = calls
        assert (request.method, request.url.path) == ("GET", "/api/agents/1")

    def test_get_status_for_missing_agent_raises(self, db_conn: psycopg.Connection) -> None:
        with pytest.raises(AgentNotFound):
            meiva.agents.get_status(999999)

    @pytest.mark.parametrize("fork_source_agent_id", [None, 3])
    def test_directory_row_preserves_fork_source(
        self, monkeypatch: pytest.MonkeyPatch, fork_source_agent_id: int | None
    ) -> None:
        def directory_page(**_kwargs: object) -> dict[str, object]:
            return {
                "agents": [
                    {
                        "agent_id": 9,
                        "label": "forked worker",
                        "status": "idling",
                        "spawner": "agent:8",
                        "fork_source_agent_id": fork_source_agent_id,
                        "machine": "mini",
                        "spawned_at": "2026-09-17T00:00:00Z",
                        "started_at": None,
                        "last_active_at": "2026-09-17T00:00:00Z",
                        "last_inbound_at": "2026-09-17T00:00:00Z",
                        "pid": None,
                        "heartbeat_paused_until": None,
                    }
                ],
                "next_cursor": None,
            }

        monkeypatch.setattr(gateway_client, "list_agents", directory_page)
        row = meiva.agents.list_agents().agents[0]
        assert row.fork_source_agent_id == fork_source_agent_id
        assert row.spawner == "agent:8"

    def test_agent_row_keeps_domain_fields(
        self,
        db_conn: psycopg.Connection,
        *,
        config_authority: ConfigAuthority,
        database_gate: ProcessDbGate,
        machine_identity: MachineIdentity,
    ) -> None:
        pin_agent(
            spawn_agent(
                config_authority=config_authority,
                database_gate=database_gate,
                local_machine=machine_identity,
            )
        )
        agent_id = meiva.agents.spawn(idempotency_key=str(uuid4()))
        db_conn.execute("UPDATE agents SET label = 'test-agent' WHERE id = %s", (agent_id,))
        db_conn.execute("UPDATE agents_meta SET status = 'running' WHERE id = %s", (agent_id,))
        db_conn.commit()

        page = meiva.agents.list_agents(query=str(agent_id))
        assert len(page.agents) == 1
        row = page.agents[0]
        assert row.agent_id == agent_id
        assert row.status is AgentStatus.RUNNING
        assert row.spawner == f"agent:{meiva.self.AGENT_ID}"
        assert row.fork_source_agent_id is None
        assert row.label == "test-agent"
        assert row.pid is None
        assert row.spawned_at is not None and row.last_active_at is not None
        assert row.machine
        assert f"#{agent_id}" in str(row)
        assert "test-agent" in str(row) and "machine=" in str(row)
        assert "pid=" not in str(row)


class TestSpawnConfig:
    def test_spawn_passes_validated_config(
        self, monkeypatch: pytest.MonkeyPatch, model_installation: Installation
    ) -> None:
        """spawn(config_overlay=...) passes validated config through to _client.spawn."""
        from meiva import agents

        monkeypatch.setattr(meiva, "__plugin_installation__", model_installation, raising=False)
        seen: dict[str, Any] = {}
        monkeypatch.setattr(gateway_client, "spawn", lambda **kw: seen.update(kw) or 3)  # pyright: ignore[reportUnknownArgumentType]
        monkeypatch.setattr(meiva, "AGENT_ID", 1, raising=False)
        agents.spawn(config_overlay={"llm_model": "claude-sonnet-5"}, idempotency_key=str(uuid4()))
        assert seen["config"] == {"llm_model": "claude-sonnet-5"}

    def test_spawn_rejects_preset_overlay_key(
        self, monkeypatch: pytest.MonkeyPatch, model_installation: Installation
    ) -> None:
        from base.packages.plugins.plugin_errors import InvalidConfigOverlay
        from meiva import agents

        monkeypatch.setattr(meiva, "__plugin_installation__", model_installation, raising=False)
        monkeypatch.setattr(meiva, "AGENT_ID", 1, raising=False)
        with pytest.raises(InvalidConfigOverlay):
            agents.spawn(
                config_overlay={"preset": "coder", "llm_model": "claude-sonnet-5"},
                idempotency_key=str(uuid4()),
            )

    def test_spawn_rejects_non_per_agent_config(
        self, monkeypatch: pytest.MonkeyPatch, model_installation: Installation
    ) -> None:
        """spawn(config_overlay=...) rejects fields not marked per_agent — raises before spawning."""
        from base.packages.plugins.plugin_errors import InvalidConfigOverlay
        from meiva import agents

        monkeypatch.setattr(meiva, "__plugin_installation__", model_installation, raising=False)
        monkeypatch.setattr(meiva, "AGENT_ID", 1, raising=False)
        with pytest.raises(InvalidConfigOverlay):
            agents.spawn(config_overlay={"db_url": "postgres://nope"}, idempotency_key=str(uuid4()))


class TestResurrect:
    def test_resurrect_returns_resurrect_result(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """resurrect(agent_id, prompt) calls gateway client and wraps status."""
        from base.api_contracts.agent_contract import ResurrectResult
        from meiva import agents

        seen: dict[str, Any] = {}
        monkeypatch.setattr(
            gateway_client,
            "resurrect",
            lambda agent_id, **kw: seen.update({"agent_id": agent_id, **kw}) or "spawned",  # pyright: ignore[reportUnknownArgumentType]
        )

        result = agents.resurrect(42, "wake up!")
        assert result == ResurrectResult.SPAWNED
        assert seen["agent_id"] == 42
        assert seen["prompt"] == "wake up!"
        # resurrected_by is set by the client default, not by the SDK wrapper
        assert "resurrected_by" not in seen

    def test_resurrect_joins_tuple_prompt_before_gateway_call(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from base.api_contracts.agent_contract import ResurrectResult
        from meiva import agents

        seen: dict[str, Any] = {}
        monkeypatch.setattr(
            gateway_client,
            "resurrect",
            lambda agent_id, **kw: seen.update({"agent_id": agent_id, **kw}) or "spawned",  # pyright: ignore[reportUnknownArgumentType]
        )

        result = agents.resurrect(42, ("wake",))  # pyright: ignore[reportArgumentType]
        assert result == ResurrectResult.SPAWNED
        assert seen["prompt"] == "wake"

    def test_resurrect_already_alive(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """When the agent is still alive, resurrect returns ALREADY_ALIVE."""
        from base.api_contracts.agent_contract import ResurrectResult
        from meiva import agents

        monkeypatch.setattr(gateway_client, "resurrect", lambda _agent_id, **_kw: "already_alive")  # pyright: ignore[reportUnknownArgumentType]
        assert agents.resurrect(1, "ping") == ResurrectResult.ALREADY_ALIVE

    def test_resurrect_prompt_is_required_positional(self) -> None:
        """prompt is a required positional arg at the SDK level."""
        import inspect

        from meiva import agents

        sig = inspect.signature(agents.resurrect)
        params = list(sig.parameters.values())
        # agent_id: positional-only-like (no default), prompt: positional-only-like (no default)
        assert params[0].name == "agent_id"
        assert params[0].default is inspect.Parameter.empty
        assert params[1].name == "prompt"
        assert params[1].default is inspect.Parameter.empty


class TestLifecycleResultEnums:
    """TerminateResult/RestartResult must stay in lockstep with the gateway's
    shared enum ownership and schema values — divergent wire values would make
    the SDK ValueError on a successful response."""

    def test_terminate_result_matches_gateway_schema(self) -> None:
        from typing import get_type_hints

        from base.api_contracts.agent_contract import TerminateResult
        from ops.rpc_schemas import TerminateAgentResponse

        assert get_type_hints(TerminateAgentResponse)["status"] is TerminateResult
        schema = TerminateAgentResponse.model_json_schema()
        reference = schema["properties"]["status"]["$ref"]
        assert reference.startswith("#/$defs/")
        values = schema["$defs"][reference.removeprefix("#/$defs/")]["enum"]
        assert values
        assert {m.value for m in TerminateResult} == set(values)

    def test_resurrect_result_matches_gateway_schema(self) -> None:
        from typing import get_type_hints

        from base.api_contracts.agent_contract import ResurrectResult
        from ops.rpc_schemas import ResurrectAgentResponse

        assert get_type_hints(ResurrectAgentResponse)["status"] is ResurrectResult
        schema = ResurrectAgentResponse.model_json_schema()
        reference = schema["properties"]["status"]["$ref"]
        assert reference.startswith("#/$defs/")
        values = schema["$defs"][reference.removeprefix("#/$defs/")]["enum"]
        assert values
        assert {m.value for m in ResurrectResult} == set(values)

    def test_restart_result_matches_gateway_schema(self) -> None:
        from typing import get_type_hints

        from base.api_contracts.agent_contract import RestartResult
        from ops.rpc_schemas import RestartAgentResponse

        assert get_type_hints(RestartAgentResponse)["status"] is RestartResult
        schema = RestartAgentResponse.model_json_schema()
        reference = schema["properties"]["status"]["$ref"]
        assert reference.startswith("#/$defs/")
        values = schema["$defs"][reference.removeprefix("#/$defs/")]["enum"]
        assert values
        assert {m.value for m in RestartResult} == set(values)
