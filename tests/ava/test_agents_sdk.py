"""`ava.agents.spawn` / `.send_message` SDK entry point tests.

Low-level lifecycle (spawn_agent / resurrect_agent / respawn_agent) covered by
`tests/gateway/test_agents_internals.py`; here only test SDK wrapper:
  - Automatically set spawner=f"agent:{ava.self.AGENT_ID}" into new agent
  - Call underlying gateway HTTP routes (via in-process TestClient going through real endpoint
    + real DB, exercising full link wire protocol)
  - resurrect automatically passes resurrected_by=f"agent:{ava.self.AGENT_ID}", underlying INSERT
    lifecycle 'resurrect' inbound + optional chat prompt same transaction
  - send_message posts inbound (source=f'agent:{ava.self.AGENT_ID}'), idling agent
    sleep 1s then recheck status

`gateway._agent_launch._launch_agent_process` monkeypatch (not actually start a process); SDK's
httpx client redirected to in-process FastAPI app via autouse fixture.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg
import pytest
from fastapi.testclient import TestClient

import ava
from ava.agents import AgentNotFound, AgentStatus, ForkSourceEmpty
from gateway import loki_events
from tests.gateway.loki_fake import FakeLoki


def _spawn_agent() -> int:
    """Setup helper — a row for the SDK's self identity (Task #1236 split: the
    row is created by create_agent_row; nothing launches, these tests only need
    the row to exist)."""
    from ops.agent_spawn import create_agent_row
    from shared.machine import machine_name

    agent_id, _ = create_agent_row(machine=machine_name())
    return agent_id


@pytest.fixture(autouse=True)
def _sdk_via_inprocess_gateway(monkeypatch: pytest.MonkeyPatch):
    """SDK ↔ Gateway path in-process test apparatus:
    1. monkeypatch session noop — spawn / resurrect / respawn don't really start child python
    2. TestClient(app) starts lifespan (build db_pool etc.), mount it as ava SDK's
       httpx client — SDK calls go through ASGI directly into gateway endpoint, real DB real logic,
       not bound to TCP port
    """
    from gateway.app import app
    from gateway.routers import agents as _agents_router
    from gateway.routers import agents_forward as _agents_forward_router
    from ops.ops_lifecycle import launch_agent_op, lifecycle_op
    from ops.rpc_schemas import LaunchAgentRequest, SpawnedAgent
    from shared import machines as _machines
    from shared.machine import machine_name

    # POST /api/agents always forwards the launch to a runner's ops server over
    # HTTP, even for the co-located box. There is no live ops server in-process,
    # so stand in for the runner's ops daemon: dispatch launch_agent_op in-process
    # against the gateway's db_pool (exactly what the daemon does on receiving the
    # forwarded op), so the SDK spawn yields a real local agent row.
    async def _in_process_forward(target: str, body: LaunchAgentRequest) -> SpawnedAgent:
        return await launch_agent_op(body, app.state.db_pool)

    # Same pattern for lifecycle ops (terminate / resurrect / restart): the
    # runner's ops daemon dispatches lifecycle_op in-process; mirror that here
    # so a forwarded local lifecycle call executes against the test DB.
    async def _in_process_lifecycle(
        target: str, path: str, json_body: dict[str, Any]
    ) -> dict[str, Any]:
        # model_dump mirrors the daemon serializing the response model onto the wire.
        return (await lifecycle_op(path, json_body, app.state.db_pool)).model_dump(mode="json")

    # post_agents reads the target's capability from the registry; the SDK targets
    # the local machine, so resolve it to agent-runner as register_self would.
    real_lookup_role = _machines.lookup_role

    def _lookup_role(name: str) -> list[str]:
        if name == machine_name():
            return ["gateway", "agent-runner"]
        return real_lookup_role(name)

    # The spawn preflight also reads the pause latch for the same target; the
    # local machine is never paused in tests, so stub it alongside the role.
    real_is_paused = _machines.is_paused

    def _is_paused(name: str) -> bool:
        if name == machine_name():
            return False
        return real_is_paused(name)

    # Mock all API keys so spawn validation passes — these tests exercise
    # the full gateway spawn path, which validates model config before forwarding.
    from pydantic import SecretStr

    from shared.config import settings as _settings

    for _attr in (
        "anthropic_api_key",
        "deepseek_api_key",
        "gemini_api_key",
        "openai_api_key",
        "xiaomi_api_key",
        "moonshot_api_key",
        "zhipu_api_key",
        "dashscope_api_key",
    ):
        monkeypatch.setattr(_settings.lm, _attr, SecretStr("sk-test"))

    with TestClient(app, base_url="http://test-gateway") as tc:
        monkeypatch.setattr("ava._gateway_transport._client", tc)
        monkeypatch.setattr(_agents_router, "_forward_spawn_to_remote", _in_process_forward)
        monkeypatch.setattr(_agents_forward_router, "_enqueue_lifecycle", _in_process_lifecycle)
        monkeypatch.setattr(_machines, "lookup_role", _lookup_role)
        monkeypatch.setattr(_machines, "is_paused", _is_paused)
        yield


def _agent_spawner(db: psycopg.Connection, agent_id: int) -> str:
    with db.cursor() as cur:
        cur.execute("SELECT spawner FROM agents_meta WHERE id = %s", (agent_id,))
        row = cur.fetchone()
    assert row is not None
    return row[0]


def _inbound_rows(db: psycopg.Connection, agent_id: int) -> list[tuple]:
    with db.cursor() as cur:
        cur.execute(
            "SELECT content, kind, source FROM inbound_messages "
            "WHERE agent_id = %s ORDER BY id ASC",
            (agent_id,),
        )
        return cur.fetchall()


class TestSpawn:
    def test_spawn_no_prompt_just_lifecycle(self, db_conn: psycopg.Connection) -> None:
        """ava.agents.spawn() without prompt — only starts lifecycle, no inbound posted."""
        ava._boot._agent_id = _spawn_agent()  # self identity

        child_id = ava.agents.spawn()

        assert _agent_spawner(db_conn, child_id) == f"agent:{ava.self.AGENT_ID}"
        assert _inbound_rows(db_conn, child_id) == []

    def test_spawn_with_prompt_inserts_inbound_with_agent_source(
        self, db_conn: psycopg.Connection
    ) -> None:
        """spawn(prompt=...) together INSERT chat inbound (source='agent:{ava.self.AGENT_ID}')."""
        ava._boot._agent_id = _spawn_agent()  # self identity

        child_id = ava.agents.spawn(prompt="\u53bb\u67e5 X")

        assert _inbound_rows(db_conn, child_id) == [
            ("\u53bb\u67e5 X", "chat", f"agent:{ava.self.AGENT_ID}"),
        ]

    def test_spawn_defaults_machine_to_local(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """When machine omitted, SDK defaults to local machine (ava.self.MACHINE_SPEC), gateway receives explicit target, no longer falls back to gateway's own machine."""
        from shared.machine import machine_name

        captured: dict[str, Any] = {}

        def _fake_spawn(
            *,
            spawner: str,
            prompt: object,
            fork_from: object,
            prompt_source: str,
            machine: str,
            config: object = None,
            label: object = None,
            preset: object = None,
        ) -> int:
            captured["machine"] = machine
            return 999

        monkeypatch.setattr(ava._gateway_client, "spawn", _fake_spawn)

        assert ava.agents.spawn() == 999
        assert captured["machine"] == machine_name()

    def test_spawn_explicit_machine_passthrough(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Explicit machine passthrough unchanged, not overridden by local default."""
        captured: dict[str, Any] = {}
        monkeypatch.setattr(ava._gateway_client, "spawn", lambda **kw: captured.update(kw) or 7)  # pyright: ignore[reportUnknownArgumentType]

        assert ava.agents.spawn(machine="other-host") == 7
        assert captured["machine"] == "other-host"

    @pytest.mark.parametrize(
        ("prompt", "expected"),
        [
            # Runtime value of `("a" "b",)`: implicit concatenation plus trailing comma.
            pytest.param(("ab",), "ab", id="implicit-concatenation-tuple"),
            pytest.param(["ab"], "ab", id="single-string-list"),
            pytest.param("ok", "ok", id="plain-string"),
        ],
    )
    def test_spawn_normalizes_prompt_before_gateway_call(
        self,
        monkeypatch: pytest.MonkeyPatch,
        prompt: object,
        expected: str,
    ) -> None:
        from ava import agents

        seen: dict[str, Any] = {}
        monkeypatch.setattr(agents._client, "spawn", lambda **kw: seen.update(kw) or 3)  # pyright: ignore[reportUnknownArgumentType]

        assert agents.spawn(prompt=prompt) == 3  # pyright: ignore[reportArgumentType]
        assert seen["prompt"] == expected

    def test_spawn_rejects_non_string_prompt(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from ava import agents

        monkeypatch.setattr(agents._client, "spawn", lambda **_kw: 3)  # pyright: ignore[reportUnknownArgumentType]

        with pytest.raises(
            TypeError,
            match="prompt must be a string, got int",
        ):
            agents.spawn(prompt=42)  # pyright: ignore[reportArgumentType]

    @pytest.mark.parametrize(
        "prompt",
        [
            pytest.param(("a", "b"), id="multi-string-tuple"),
            pytest.param(["a", "b"], id="multi-string-list"),
        ],
    )
    def test_spawn_rejects_multi_element_prompt(
        self, monkeypatch: pytest.MonkeyPatch, prompt: object
    ) -> None:
        """A multi-element prompt sequence is a coding mistake — TypeError, never
        silently joined (user ruling 2026-08-28)."""
        from ava import agents

        monkeypatch.setattr(agents._client, "spawn", lambda **_kw: 3)  # pyright: ignore[reportUnknownArgumentType]
        with pytest.raises(TypeError, match="prompt must be a string"):
            agents.spawn(prompt=prompt)  # pyright: ignore[reportArgumentType]


class TestSpawnFork:
    def test_fork_resolves_latest_checkpoint(self, db_conn: psycopg.Connection) -> None:
        """ava.agents.spawn(fork_from=N) internally resolves latest checkpoint
        (done by gateway, SDK unaware of ckpt id)."""
        ava._boot._agent_id = _spawn_agent()  # self identity
        source = ava.agents.spawn()
        # construct chain a < b < c (lex order corresponds to time order)
        with db_conn.cursor() as cur:
            for ckpt, parent in [("ck-a", None), ("ck-b", "ck-a"), ("ck-c", "ck-b")]:
                cur.execute(
                    "INSERT INTO checkpoints (thread_id, checkpoint_id, parent_checkpoint_id, "
                    "checkpoint, metadata) VALUES (%s, %s, %s, '{}'::jsonb, '{}'::jsonb)",
                    (str(source), ckpt, parent),
                )
        db_conn.commit()

        new_id = ava.agents.spawn(fork_from=source)

        # fork_source_checkpoint_id should be set to latest = "ck-c"
        with db_conn.cursor() as cur:
            cur.execute(
                "SELECT fork_source_agent_id, fork_source_checkpoint_id FROM agents_meta WHERE id = %s",
                (new_id,),
            )
            row = cur.fetchone()
        assert row == (source, "ck-c")

    def test_fork_no_checkpoint_raises_fork_source_empty(self, db_conn: psycopg.Connection) -> None:
        """fork_from source has no checkpoint → ForkSourceEmpty.

        wire path: gateway side resolve_latest_checkpoint_id gets None → raise
        ForkSourceEmpty → handler converts to 409 + reason="fork_source_empty" → SDK
        `_raise_from_response` reverse lookup rebuild.
        """
        ava._boot._agent_id = _spawn_agent()  # self identity
        empty_source = ava.agents.spawn()  # spawn without checkpoint
        _ = db_conn  # truncate side-effect via fixture

        with pytest.raises(ForkSourceEmpty, match="has no checkpoint"):
            ava.agents.spawn(fork_from=empty_source)

    def test_fork_with_prompt_inserts_fork_identity_then_chat_inbound(
        self, db_conn: psycopg.Connection
    ) -> None:
        """ava.agents.spawn(prompt=..., fork_from=source) first posts fork identity inbound
        (kind='fork', source=f"agent:{source}"), then prompt's chat inbound —
        claim side first dispatches identity marker to fix "who am I", then processes prompt."""
        ava._boot._agent_id = _spawn_agent()  # self identity
        source = ava.agents.spawn()
        # give source a checkpoint
        with db_conn.cursor() as cur:
            cur.execute(
                "INSERT INTO checkpoints (thread_id, checkpoint_id, parent_checkpoint_id, "
                "checkpoint, metadata) VALUES (%s, 'ck1', NULL, '{}'::jsonb, '{}'::jsonb)",
                (str(source),),
            )
        db_conn.commit()

        new_id = ava.agents.spawn(prompt="\u7ee7\u7eed\u5427", fork_from=source)

        # fork first posts an identity inbound (kind='fork', source=fork source agent) in spawn transaction, then prompt's chat inbound —
        # claim side dispatches identity marker first to fix "who am I", then processes prompt.
        assert _inbound_rows(db_conn, new_id) == [
            ("", "fork", f"agent:{source}"),
            ("\u7ee7\u7eed\u5427", "chat", f"agent:{ava.self.AGENT_ID}"),
        ]


class TestTerminate:
    def test_message_is_queued_before_terminate_with_agent_source(
        self, db_conn: psycopg.Connection
    ) -> None:
        ava._boot._agent_id = _spawn_agent()
        peer_id = ava.agents.spawn()

        result = ava.agents.terminate(peer_id, message="record the partial result")

        assert result == "enqueued"
        assert _inbound_rows(db_conn, peer_id) == [
            ("record the partial result", "chat", f"agent:{ava.self.AGENT_ID}"),
            ("", "terminate", f"agent:{ava.self.AGENT_ID}"),
        ]

    def test_rejects_non_string_message_before_gateway_call(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        called = False

        def _terminate(*_args: object, **_kwargs: object) -> str:
            nonlocal called
            called = True
            return "enqueued"

        monkeypatch.setattr(ava.agents._client, "terminate", _terminate)
        with pytest.raises(TypeError, match="message must be a string, got int"):
            ava.agents.terminate(7, message=42)  # pyright: ignore[reportArgumentType]
        assert not called


class TestSendMessage:
    def test_send_message_inserts_chat_inbound_with_agent_source(
        self, db_conn: psycopg.Connection
    ) -> None:
        """send_message purely INSERT inbound — no status check, no wait, no SendResult return."""
        ava._boot._agent_id = _spawn_agent()
        peer_id = ava.agents.spawn()

        result = ava.agents.send_message(peer_id, "you got mail")
        assert result is None

        assert _inbound_rows(db_conn, peer_id) == [
            ("you got mail", "chat", f"agent:{ava.self.AGENT_ID}"),
        ]

    def test_send_message_to_terminated_is_fine(self, db_conn: psycopg.Connection) -> None:
        """send_message to terminated agent also INSERT inbound.
        SDK doesn't care about target state — purely send message, auto-resurrect is gateway-side detail."""
        ava._boot._agent_id = _spawn_agent()
        peer_id = ava.agents.spawn()
        with db_conn.cursor() as cur:
            cur.execute("UPDATE agents_meta SET status = 'terminated' WHERE id = %s", (peer_id,))
        db_conn.commit()

        result = ava.agents.send_message(peer_id, "still works")
        assert result is None
        # Chat inbound was inserted — that's all the SDK cares about.
        rows = _inbound_rows(db_conn, peer_id)
        assert ("still works", "chat", f"agent:{ava.self.AGENT_ID}") in rows

    def test_send_message_to_nonexistent_raises(
        self,
        db_conn: psycopg.Connection,
    ) -> None:
        with pytest.raises(AgentNotFound):
            ava.agents.send_message(9999, "ghost")

    def test_send_message_does_not_touch_agents_lifecycle(
        self, db_conn: psycopg.Connection
    ) -> None:
        """send_message only INSERT inbound, doesn't modify agents.status."""
        ava._boot._agent_id = _spawn_agent()
        peer_id = ava.agents.spawn()
        ava.agents.send_message(peer_id, "hi")

    @pytest.mark.parametrize(
        ("content", "expected"),
        [
            # Runtime value of `("ab",)`: implicit concatenation plus trailing comma.
            pytest.param(("ab",), "ab", id="implicit-concatenation-tuple"),
            pytest.param(["ab"], "ab", id="single-string-list"),
            pytest.param("ok", "ok", id="plain-string"),
        ],
    )
    def test_send_message_normalizes_tuple_content_before_gateway_call(
        self,
        monkeypatch: pytest.MonkeyPatch,
        content: object,
        expected: str,
    ) -> None:
        """A trailing-comma string tuple must reach the client as the string it
        wraps — an all-string array on the wire 422s the gateway's
        `AgentMessageIn.content` (2026-08-28 agents 2697/2986)."""
        from ava import agents

        seen: dict[str, Any] = {}
        monkeypatch.setattr(
            agents._client,
            "send_message",
            lambda _agent_id, **kw: seen.update(kw) or None,  # pyright: ignore[reportUnknownArgumentType]
        )

        agents.send_message(7, content)  # pyright: ignore[reportArgumentType]
        assert seen["content"] == expected
        assert seen["source"] == f"agent:{ava.self.AGENT_ID}"

    @pytest.mark.parametrize(
        "content",
        [
            pytest.param(("a", "b"), id="multi-string-tuple"),
            pytest.param(["a", "b"], id="multi-string-list"),
            pytest.param((1,), id="non-string-element"),
            pytest.param(42, id="int"),
        ],
    )
    def test_send_message_rejects_ambiguous_content(
        self, monkeypatch: pytest.MonkeyPatch, content: object
    ) -> None:
        """A multi-element string sequence is a coding mistake, not a message —
        it fails loud with TypeError instead of being silently joined (user
        ruling 2026-08-28: multi-element sequences raise TypeError)."""
        from ava import agents

        monkeypatch.setattr(agents._client, "send_message", lambda *_a, **_kw: None)  # pyright: ignore[reportUnknownArgumentType]
        with pytest.raises(TypeError, match="content must be a string"):
            agents.send_message(7, content)  # pyright: ignore[reportArgumentType]

    def test_send_message_content_blocks_pass_through(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A list of content-block dicts (multimodal path) is not string-joined."""
        from ava import agents

        seen: dict[str, Any] = {}
        monkeypatch.setattr(
            agents._client,
            "send_message",
            lambda _agent_id, **kw: seen.update(kw) or None,  # pyright: ignore[reportUnknownArgumentType]
        )
        blocks: list[dict[str, object]] = [
            {"type": "text", "text": "hi"},
            {"type": "image_url", "image_url": {"url": "u"}},
        ]
        agents.send_message(7, blocks)  # pyright: ignore[reportArgumentType]
        assert seen["content"] == blocks

    def test_send_message_rejects_non_string_content(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from ava import agents

        monkeypatch.setattr(agents._client, "send_message", lambda *_a, **_kw: None)  # pyright: ignore[reportUnknownArgumentType]
        with pytest.raises(
            TypeError,
            match="content must be a string, got int",
        ):
            agents.send_message(7, 42)  # pyright: ignore[reportArgumentType]

    def test_send_message_tuple_content_inserts_unwrapped_inbound(
        self, db_conn: psycopg.Connection
    ) -> None:
        """End-to-end: the trailing-comma tuple lands as the string it wraps,
        never as a JSON array (which the gateway would reject 422)."""
        ava._boot._agent_id = _spawn_agent()
        peer_id = ava.agents.spawn()

        # Runtime value of `("you got " "mail",)`: implicit concatenation plus
        # trailing comma — the exact LLM shape that 422'd before (2026-08-28).
        content: object = ("you got mail",)
        ava.agents.send_message(peer_id, content)  # pyright: ignore[reportArgumentType]

        assert _inbound_rows(db_conn, peer_id) == [
            ("you got mail", "chat", f"agent:{ava.self.AGENT_ID}"),
        ]


class TestSendSystemNote:
    def test_send_system_note_inserts_system_note_inbound_with_task_tag(
        self, db_conn: psycopg.Connection
    ) -> None:
        """send_system_note posts a kind='system_note' inbound (agent source +
        task note tag) — never a peer chat row."""
        ava._boot._agent_id = _spawn_agent()
        peer_id = ava.agents.spawn()

        inbound_id = ava.agents.send_system_note(
            peer_id, 'Task #1 "t" is now assigned to you (by agent #1).'
        )
        assert isinstance(inbound_id, int)

        with db_conn.cursor() as cur:
            cur.execute(
                "SELECT content, kind, source, payload FROM inbound_messages WHERE agent_id = %s",
                (peer_id,),
            )
            rows = cur.fetchall()
        assert len(rows) == 1
        content, kind, source, payload = rows[0]
        assert kind == "system_note"
        assert source == f"agent:{ava.self.AGENT_ID}"
        assert "assigned to you" in content
        assert payload == {"note_tag": "task"}

    def test_send_system_note_preserves_explicit_task_id(self, db_conn: psycopg.Connection) -> None:
        ava._boot._agent_id = _spawn_agent()
        peer_id = ava.agents.spawn()
        with db_conn.cursor() as cur:
            cur.execute(
                "INSERT INTO agent_tasks (title, description, created_by, owner) "
                "VALUES ('task note target', 'd', 'user', %s) RETURNING id",
                (peer_id,),
            )
            row = cur.fetchone()
        assert row is not None
        task_id = row[0]
        db_conn.commit()

        ava.agents.send_system_note(
            peer_id, 'Task #42 "t" is now assigned to you.', task_id=task_id
        )

        with db_conn.cursor() as cur:
            cur.execute("SELECT payload FROM inbound_messages WHERE agent_id = %s", (peer_id,))
            row = cur.fetchone()
        assert row is not None
        assert row[0] == {"note_tag": "task", "task_id": task_id}

    def test_send_system_note_to_terminated_is_fine(self, db_conn: psycopg.Connection) -> None:
        """A note with resurrect=True (task assignment) reaches a terminated
        agent — auto-resurrect is the gateway delivery detail, the SDK just
        posts the note and returns its id."""
        ava._boot._agent_id = _spawn_agent()
        peer_id = ava.agents.spawn()
        with db_conn.cursor() as cur:
            cur.execute("UPDATE agents_meta SET status = 'terminated' WHERE id = %s", (peer_id,))
        db_conn.commit()

        inbound_id = ava.agents.send_system_note(peer_id, 'Task #1 "t" is now assigned to you.')
        assert isinstance(inbound_id, int)
        with db_conn.cursor() as cur:
            cur.execute("SELECT kind FROM inbound_messages WHERE agent_id = %s", (peer_id,))
            kinds = [row[0] for row in cur.fetchall()]
        assert "system_note" in kinds

    def test_send_system_note_normalizes_tuple_content(self, db_conn: psycopg.Connection) -> None:
        """Same trailing-comma class as send_message — a one-element tuple
        unwraps to the note text instead of 422ing the gateway."""
        ava._boot._agent_id = _spawn_agent()
        peer_id = ava.agents.spawn()

        # Runtime value of `("Task #1 is now " "assigned to you.",)`: implicit
        # concatenation plus trailing comma.
        content: object = ("Task #1 is now assigned to you.",)
        inbound_id = ava.agents.send_system_note(peer_id, content)  # pyright: ignore[reportArgumentType]
        assert isinstance(inbound_id, int)
        with db_conn.cursor() as cur:
            cur.execute("SELECT content FROM inbound_messages WHERE agent_id = %s", (peer_id,))
            rows = cur.fetchall()
        assert len(rows) == 1
        assert rows[0][0] == "Task #1 is now assigned to you."

    def test_send_system_note_rejects_multi_element_content(
        self, db_conn: psycopg.Connection
    ) -> None:
        """A multi-element tuple is a coding mistake — TypeError, never joined."""
        ava._boot._agent_id = _spawn_agent()
        peer_id = ava.agents.spawn()

        content: object = ("Task #1 is now ", "assigned to you.")
        with pytest.raises(TypeError, match="content must be a string"):
            ava.agents.send_system_note(peer_id, content)  # pyright: ignore[reportArgumentType]

    def test_send_system_note_to_nonexistent_raises(self, db_conn: psycopg.Connection) -> None:
        ava._boot._agent_id = _spawn_agent()
        with pytest.raises(AgentNotFound):
            ava.agents.send_system_note(9999, "ghost")


class TestGetNeighbors:
    """SDK get_neighbors maps the gateway rows to Neighbor dataclasses (status to
    the AgentStatus enum). The graph behaviors (Loki live tail + archive stitch)
    are covered in tests/gateway/test_agent_neighbors.py; here we verify the
    wrapper + wire path only, seeding ties through the FakeLoki live tail."""

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
    def _tie(
        fake: FakeLoki,
        agent_id: int,
        target: int,
        *,
        days_ago: float | None = None,
        ts: datetime | None = None,
        archive: bool = False,
    ) -> None:
        if ts is None:
            assert days_ago is not None
            ts = datetime.now(UTC) - timedelta(hours=days_ago * 24.0)
        fake.add(
            event="send_message",
            agent_id=agent_id,
            target_agent_id=target,
            category="audit",
            ts=ts,
            archive=archive,
        )

    def test_returns_ranked_neighbor_dataclasses(
        self, db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake = FakeLoki()
        monkeypatch.setattr(loki_events, "query_events", fake.query_events)
        a = self._seed(db_conn)
        fresh = self._seed(db_conn)
        stale = self._seed(db_conn, status="terminated")
        self._tie(fake, fresh, a, days_ago=0.0)
        # The stale tie sits inside the archive span (before ARCHIVE_FREEZE_AT)
        # and must be seeded as an archive-stream row: the live-stream query is
        # clamped at the freeze point, so a live row there would fall in the gap.
        self._tie(fake, stale, a, ts=datetime(2026, 8, 10, tzinfo=UTC), archive=True)

        rows = ava.agents.get_neighbors(a)

        assert all(isinstance(r, ava.agents.Neighbor) for r in rows)
        assert [r.agent_id for r in rows] == [fresh, stale]  # recent ranks above stale
        assert rows[1].status is AgentStatus.TERMINATED  # terminated neighbor included
        assert rows[0].depth == 1
        assert rows[0].score > rows[1].score
        assert f"#{fresh}" in str(rows[0]) and "depth=1" in str(rows[0])

    def test_nonexistent_raises(self, db_conn: psycopg.Connection) -> None:
        with pytest.raises(AgentNotFound):
            ava.agents.get_neighbors(9999)


class TestGetAncestors:
    """SDK get_ancestors maps the gateway `ancestors` rows to Neighbor
    dataclasses. The chain walk itself is covered in
    tests/gateway/test_agent_neighbors.py; here we verify the wrapper + wire
    path only. Ancestry is the immutable `agents_meta.born_spawner` chain, so
    it is seeded on the row; the FakeLoki spawn event only feeds the tie
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
    def _spawn(fake: FakeLoki, child: int, parent: int) -> None:
        # Event direction: agent_id = the new agent, target_agent_id = spawner.
        fake.add(
            event="spawn",
            agent_id=child,
            target_agent_id=parent,
            category="audit",
            ts_offset_hours=0.0,
        )

    def test_returns_spawn_chain_as_neighbor_dataclasses(
        self, db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake = FakeLoki()
        monkeypatch.setattr(loki_events, "query_events", fake.query_events)
        a = self._seed(db_conn, status="terminated")
        b = self._seed(db_conn, born_spawner=f"agent:{a}")
        self._spawn(fake, b, a)

        rows = ava.agents.get_ancestors(b)

        assert all(isinstance(r, ava.agents.Neighbor) for r in rows)
        assert [r.agent_id for r in rows] == [a]  # nearest ancestor first
        assert rows[0].depth == 1  # hops UP from the queried agent
        assert rows[0].status is AgentStatus.TERMINATED  # terminated parent included
        assert f"#{a}" in str(rows[0]) and "depth=1" in str(rows[0])

    def test_no_spawner_returns_empty(
        self, db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake = FakeLoki()
        monkeypatch.setattr(loki_events, "query_events", fake.query_events)
        a = self._seed(db_conn)

        assert ava.agents.get_ancestors(a) == []

    def test_nonexistent_raises(self, db_conn: psycopg.Connection) -> None:
        with pytest.raises(AgentNotFound):
            ava.agents.get_ancestors(9999)


class TestListAgents:
    def test_gateway_client_requests_summary_projection(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The SDK preserves its row shape without requesting the full snapshot."""
        import ava._gateway_client as gateway_client

        seen: dict[str, object] = {}

        class _Response:
            def json(self) -> list[dict[str, object]]:
                return []

        def fake_get(path: str, *, params: dict[str, object]) -> _Response:
            seen["path"] = path
            seen["params"] = params
            return _Response()

        def fake_raise(_response: object) -> None:
            return None

        monkeypatch.setattr(gateway_client, "_get", fake_get)
        monkeypatch.setattr(gateway_client, "_raise_from_response", fake_raise)

        assert gateway_client.list_agents((AgentStatus.RUNNING, AgentStatus.IDLING)) == []
        assert seen == {
            "path": "/api/agents",
            "params": {"scope": "live", "fields": "summary"},
        }

    def test_default_filter_returns_running_and_idling(self, db_conn: psycopg.Connection) -> None:
        """Default filter=(RUNNING, IDLING), only returns these two status agents."""
        ava._boot._agent_id = _spawn_agent()
        a_id = ava.agents.spawn()
        b_id = ava.agents.spawn()
        c_id = ava.agents.spawn()  # terminal rows stay outside the default live filter
        with db_conn.cursor() as cur:
            cur.execute(
                "UPDATE agents_meta SET status = 'terminated' WHERE id = %s", (ava.self.AGENT_ID,)
            )
            cur.execute("UPDATE agents_meta SET status = 'running' WHERE id = %s", (a_id,))
            cur.execute("UPDATE agents_meta SET status = 'idling' WHERE id = %s", (b_id,))
            cur.execute("UPDATE agents_meta SET status = 'terminated' WHERE id = %s", (c_id,))
        db_conn.commit()

        rows = ava.agents.list_agents()
        ids = {r.agent_id for r in rows}
        assert ids == {a_id, b_id}
        assert all(
            r.status in (ava.agents.AgentStatus.RUNNING, ava.agents.AgentStatus.IDLING)
            for r in rows
        )

    def test_custom_filter_returns_matching_only(self, db_conn: psycopg.Connection) -> None:
        """Explicitly passing filter_by_status only returns matching ones."""
        ava._boot._agent_id = _spawn_agent()
        a_id = ava.agents.spawn()
        b_id = ava.agents.spawn()
        with db_conn.cursor() as cur:
            cur.execute("UPDATE agents_meta SET status = 'running' WHERE id = %s", (a_id,))
            cur.execute("UPDATE agents_meta SET status = 'terminated' WHERE id = %s", (b_id,))
        db_conn.commit()

        rows = ava.agents.list_agents(filter_by_status=(ava.agents.AgentStatus.TERMINATED,))
        assert [r.agent_id for r in rows] == [b_id]

    def test_empty_list_when_no_match(
        self,
        db_conn: psycopg.Connection,
    ) -> None:
        """When no matching agent, returns empty list."""
        with db_conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM agents_meta WHERE status IN ('running','idling')")
            row = cur.fetchone()
            assert row is not None
            count = row[0]
        assert count == 0
        assert ava.agents.list_agents() == []

    def test_empty_tuple_returns_all_agents(self, db_conn: psycopg.Connection) -> None:
        """Empty tuple equivalent to None / no filter, returns all agents."""
        ava._boot._agent_id = _spawn_agent()
        a_id = ava.agents.spawn()
        b_id = ava.agents.spawn()
        with db_conn.cursor() as cur:
            cur.execute("UPDATE agents_meta SET status = 'running' WHERE id = %s", (a_id,))
            cur.execute("UPDATE agents_meta SET status = 'terminated' WHERE id = %s", (b_id,))
        db_conn.commit()

        # empty tuple → no filter, returns all (including helper and terminated)
        rows = ava.agents.list_agents(filter_by_status=())
        ids = {r.agent_id for r in rows}
        assert a_id in ids
        assert b_id in ids
        assert len(ids) >= 2  # at least includes the two spawned agents (might include helper)

    def test_none_filter_returns_all_agents(self, db_conn: psycopg.Connection) -> None:
        """filter_by_status=None no filter, returns all agents."""
        ava._boot._agent_id = _spawn_agent()
        a_id = ava.agents.spawn()
        b_id = ava.agents.spawn()
        with db_conn.cursor() as cur:
            cur.execute("UPDATE agents_meta SET status = 'running' WHERE id = %s", (a_id,))
            cur.execute("UPDATE agents_meta SET status = 'terminated' WHERE id = %s", (b_id,))
        db_conn.commit()

        rows = ava.agents.list_agents(filter_by_status=None)
        ids = {r.agent_id for r in rows}
        assert a_id in ids
        assert b_id in ids
        assert len(ids) >= 2  # at least includes the two spawned agents

    def test_includes_terminated_when_asked(self, db_conn: psycopg.Connection) -> None:
        """Explicitly passing TERMINATED can list terminated agents."""
        ava._boot._agent_id = _spawn_agent()
        a_id = ava.agents.spawn()
        with db_conn.cursor() as cur:
            cur.execute(
                "UPDATE agents_meta SET status = 'restarting' WHERE id = %s", (ava.self.AGENT_ID,)
            )
            cur.execute("UPDATE agents_meta SET status = 'terminated' WHERE id = %s", (a_id,))
        db_conn.commit()

        rows = ava.agents.list_agents(
            filter_by_status=(
                ava.agents.AgentStatus.RUNNING,
                ava.agents.AgentStatus.IDLING,
                ava.agents.AgentStatus.TERMINATED,
            )
        )
        assert [r.agent_id for r in rows] == [a_id]

    def test_agent_row_str_hides_none_fields(self, db_conn: psycopg.Connection) -> None:
        """AgentRow.__str__ doesn't show None fields."""
        ava._boot._agent_id = _spawn_agent()
        a_id = ava.agents.spawn()
        with db_conn.cursor() as cur:
            cur.execute("UPDATE agents_meta SET status = 'running' WHERE id = %s", (a_id,))
        db_conn.commit()

        rows = ava.agents.list_agents(filter_by_status=(ava.agents.AgentStatus.RUNNING,))
        assert len(rows) == 1
        s = str(rows[0])
        assert f"#{a_id}" in s
        assert "running" in s
        # pid is None → not shown in str; label also not set
        assert "pid=" not in s

    def test_agent_row_str_shows_present_fields(self, db_conn: psycopg.Connection) -> None:
        """Fields with values should appear in __str__ output."""
        ava._boot._agent_id = _spawn_agent()
        a_id = ava.agents.spawn()
        # Set a label via the db (label is on agents table, not agents_meta)
        with db_conn.cursor() as cur:
            cur.execute(
                "UPDATE agents SET label = 'test-agent' WHERE id = %s",
                (a_id,),
            )
            cur.execute(
                "UPDATE agents_meta SET status = 'running' WHERE id = %s",
                (a_id,),
            )
        db_conn.commit()

        rows = ava.agents.list_agents(filter_by_status=(ava.agents.AgentStatus.RUNNING,))
        assert len(rows) == 1
        s = str(rows[0])
        assert "test-agent" in s
        assert "machine=" in s

    def test_agent_row_returns_full_dataclass_fields(self, db_conn: psycopg.Connection) -> None:
        """All AgentRow fields are programmatically accessible, not trimmed like str() of None."""
        ava._boot._agent_id = _spawn_agent()
        a_id = ava.agents.spawn()
        assert _agent_spawner(db_conn, a_id) == f"agent:{ava.self.AGENT_ID}"
        with db_conn.cursor() as cur:
            cur.execute(
                "UPDATE agents_meta SET status = 'terminated' WHERE id = %s", (ava.self.AGENT_ID,)
            )
            cur.execute("UPDATE agents_meta SET status = 'idling' WHERE id = %s", (a_id,))
        db_conn.commit()

        rows = ava.agents.list_agents(filter_by_status=(ava.agents.AgentStatus.IDLING,))
        assert len(rows) == 1
        r = rows[0]
        assert r.agent_id == a_id
        assert r.status == ava.agents.AgentStatus.IDLING
        # Lifecycle status transitions preserve spawn lineage (immutable spawner):
        # terminating the parent does not rewrite the child's spawn record.
        assert r.spawner == f"agent:{ava.self.AGENT_ID}"
        assert r.label is None  # no label set on this test agent
        assert r.pid is None
        assert r.spawned_at is not None
        assert r.last_active_at is not None
        # machine is not empty, default is gateway's local machine_name() (db default 'unknown'
        # only triggered by manual INSERT, spawn path brings value)
        assert r.machine and isinstance(r.machine, str)


class TestSpawnConfig:
    def test_spawn_passes_validated_config(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """spawn(config_overlay=...) passes validated config through to _client.spawn."""
        from ava import agents

        seen: dict[str, Any] = {}
        monkeypatch.setattr(agents._client, "spawn", lambda **kw: seen.update(kw) or 3)  # pyright: ignore[reportUnknownArgumentType]
        monkeypatch.setattr(ava, "AGENT_ID", 1, raising=False)
        agents.spawn(config_overlay={"llm_model": "claude-sonnet-5"})
        assert seen["config"] == {"llm_model": "claude-sonnet-5"}

    def test_spawn_passes_preset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """spawn(preset=...) forwards the preset name to _client.spawn (the gateway
        resolves it to a config template; the SDK just passes the name)."""
        from ava import agents

        seen: dict[str, Any] = {}
        monkeypatch.setattr(agents._client, "spawn", lambda **kw: seen.update(kw) or 3)  # pyright: ignore[reportUnknownArgumentType]
        monkeypatch.setattr(ava, "AGENT_ID", 1, raising=False)
        agents.spawn(preset="coder")
        assert seen["preset"] == "coder"

    def test_spawn_rejects_non_per_agent_config(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """spawn(config_overlay=...) rejects fields not marked per_agent — raises before spawning."""
        from ava import agents
        from shared.plugin_config_registry import InvalidConfigOverlay

        monkeypatch.setattr(ava, "AGENT_ID", 1, raising=False)
        with pytest.raises(InvalidConfigOverlay):
            agents.spawn(config_overlay={"db_url": "postgres://nope"})


class TestResurrect:
    def test_resurrect_returns_resurrect_result(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """resurrect(agent_id, prompt) calls gateway client and wraps status."""
        from ava import agents
        from shared.agents import ResurrectResult

        seen: dict[str, Any] = {}
        monkeypatch.setattr(
            agents._client,
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
        from ava import agents
        from shared.agents import ResurrectResult

        seen: dict[str, Any] = {}
        monkeypatch.setattr(
            agents._client,
            "resurrect",
            lambda agent_id, **kw: seen.update({"agent_id": agent_id, **kw}) or "spawned",  # pyright: ignore[reportUnknownArgumentType]
        )

        result = agents.resurrect(42, ("wake",))  # pyright: ignore[reportArgumentType]
        assert result == ResurrectResult.SPAWNED
        assert seen["prompt"] == "wake"

    def test_resurrect_already_alive(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """When the agent is still alive, resurrect returns ALREADY_ALIVE."""
        from ava import agents
        from shared.agents import ResurrectResult

        monkeypatch.setattr(agents._client, "resurrect", lambda _agent_id, **_kw: "already_alive")  # pyright: ignore[reportUnknownArgumentType]
        assert agents.resurrect(1, "ping") == ResurrectResult.ALREADY_ALIVE

    def test_resurrect_prompt_is_required_positional(self) -> None:
        """prompt is a required positional arg at the SDK level."""
        import inspect

        from ava import agents

        sig = inspect.signature(agents.resurrect)
        params = list(sig.parameters.values())
        # agent_id: positional-only-like (no default), prompt: positional-only-like (no default)
        assert params[0].name == "agent_id"
        assert params[0].default is inspect.Parameter.empty
        assert params[1].name == "prompt"
        assert params[1].default is inspect.Parameter.empty


class TestLifecycleResultEnums:
    """TerminateResult/RestartResult must stay in lockstep with the gateway's
    wire Literal unions — a new wire status added on one side only would make
    the SDK ValueError on a successful response."""

    def test_terminate_result_matches_gateway_literal(self) -> None:
        from typing import get_args, get_type_hints

        from ops.rpc_schemas import TerminateAgentResponse
        from shared.agents import TerminateResult

        literal = get_type_hints(TerminateAgentResponse)["status"]
        assert {m.value for m in TerminateResult} == set(get_args(literal))

    def test_resurrect_result_matches_gateway_literal(self) -> None:
        from typing import get_args, get_type_hints

        from ops.rpc_schemas import ResurrectAgentResponse
        from shared.agents import ResurrectResult

        literal = get_type_hints(ResurrectAgentResponse)["status"]
        assert {m.value for m in ResurrectResult} == set(get_args(literal))

    def test_restart_result_matches_gateway_literal(self) -> None:
        from typing import get_args, get_type_hints

        from ops.rpc_schemas import RestartAgentResponse
        from shared.agents import RestartResult

        literal = get_type_hints(RestartAgentResponse)["status"]
        assert {m.value for m in RestartResult} == set(get_args(literal))
