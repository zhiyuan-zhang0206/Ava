"""`ava.agents.spawn` / `.send_message` SDK entry point tests.

Low-level lifecycle (spawn_agent / resurrect_agent / respawn_agent) covered by
`ops/agents/tests/test_agents_internals.py`; here only test SDK wrapper:
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

from datetime import datetime
from typing import Any
from uuid import uuid4

import psycopg
import pytest
from fastapi.testclient import TestClient

import ava
from ava import gateway_client
from ava.agents import AgentNotFound, ForkSourceEmpty, TerminateResult
from ava.gateway_client.transport import use_client
from base.agents import ShellSessionKillTiming
from base.db import Database
from base.events.live.bus import EventBus
from tests.fixtures.pin_agent import pin_agent


def _spawn_agent() -> int:
    """Setup helper — a row for the SDK's self identity (Task #1236 split: the
    row is created by create_agent_row; nothing launches, these tests only need
    the row to exist)."""
    from base.cluster.machine import machine_name
    from ops.agents.spawn import create_agent_row

    agent_id, _, _prompt_id, _attempt_id = create_agent_row(
        Database.from_settings(), EventBus.from_settings(), machine=machine_name()
    )
    return agent_id


@pytest.fixture(autouse=True)
def _sdk_via_inprocess_gateway(
    monkeypatch: pytest.MonkeyPatch, database: Database, event_bus: EventBus
):
    """SDK ↔ Gateway path in-process test apparatus:
    1. monkeypatch session noop — spawn / resurrect / respawn don't really start child python
    2. TestClient(app) starts lifespan (build db_pool etc.), mount it as ava SDK's
       httpx client — SDK calls go through ASGI directly into gateway endpoint, real DB real logic,
       not bound to TCP port
    """
    from base.cluster import machines as _machines
    from base.cluster.machine import machine_name
    from gateway.agents import forward as _agents_forward_router
    from gateway.agents import router as _agents_router
    from gateway.app import app
    from ops.lifecycle import launch_agent_op, lifecycle_op
    from ops.rpc_schemas import LaunchAgentRequest, SpawnedAgent

    # POST /api/agents always forwards the launch to a runner's ops server over
    # HTTP, even for the co-located box. There is no live ops server in-process,
    # so stand in for the runner's ops daemon: dispatch launch_agent_op in-process
    # against the gateway's db_pool (exactly what the daemon does on receiving the
    # forwarded op), so the SDK spawn yields a real local agent row.
    async def _in_process_forward(
        _db: object, target: str, body: LaunchAgentRequest
    ) -> SpawnedAgent:
        return await launch_agent_op(database, event_bus, body, app.state.db_pool)

    # Same pattern for lifecycle ops (terminate / resurrect / restart): the
    # runner's ops daemon dispatches lifecycle_op in-process; mirror that here
    # so a forwarded local lifecycle call executes against the test DB.
    async def _in_process_lifecycle(
        _db: object, target: str, path: str, json_body: dict[str, Any]
    ) -> dict[str, Any]:
        # model_dump mirrors the daemon serializing the response model onto the wire.
        return (
            await lifecycle_op(database, event_bus, path, json_body, app.state.db_pool)
        ).model_dump(mode="json")

    # post_agents reads the target's capability from the registry; the SDK targets
    # the local machine, so resolve it to agent-runner as register_self would.
    real_lookup_role = _machines.lookup_role

    def _lookup_role(_db: Database, name: str) -> list[str]:
        if name == machine_name():
            return ["gateway", "agent-runner"]
        return real_lookup_role(_db, name)

    # The spawn preflight also reads the pause latch for the same target; the
    # local machine is never paused in tests, so stub it alongside the role.
    real_is_paused = _machines.is_paused

    def _is_paused(_db: Database, name: str) -> bool:
        if name == machine_name():
            return False
        return real_is_paused(_db, name)

    # Mock all API keys so spawn validation passes — these tests exercise
    # the full gateway spawn path, which validates model config before forwarding.
    from pydantic import SecretStr

    from base.config import settings as _settings

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

    with TestClient(app, base_url="http://test-gateway") as tc, use_client(tc):
        monkeypatch.setattr(_agents_router, "forward_spawn_to_remote", _in_process_forward)
        monkeypatch.setattr(_agents_forward_router, "enqueue_lifecycle", _in_process_lifecycle)
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
    def test_public_creation_key_recovers_birth_and_rejects_changed_body(
        self, db_conn: psycopg.Connection
    ) -> None:
        pin_agent(_spawn_agent())
        first = ava.agents.spawn(prompt="one goal", idempotency_key="public-create-a")
        assert ava.agents.spawn(prompt="one goal", idempotency_key="public-create-a") == first
        assert _inbound_rows(db_conn, first) == [("one goal", "chat", f"agent:{ava.self.AGENT_ID}")]
        assert ava.agents.spawn(prompt="one goal", idempotency_key="public-create-b") != first
        from httpx2 import HTTPStatusError

        with pytest.raises(HTTPStatusError, match="409"):
            ava.agents.spawn(prompt="different", idempotency_key="public-create-a")

    def test_spawn_no_prompt_just_lifecycle(self, db_conn: psycopg.Connection) -> None:
        """ava.agents.spawn() without prompt — only starts lifecycle, no inbound posted."""
        pin_agent(_spawn_agent())  # self identity

        child_id = ava.agents.spawn()

        assert _agent_spawner(db_conn, child_id) == f"agent:{ava.self.AGENT_ID}"
        assert _inbound_rows(db_conn, child_id) == []

    def test_spawn_with_prompt_inserts_inbound_with_agent_source(
        self, db_conn: psycopg.Connection
    ) -> None:
        """spawn(prompt=...) together INSERT chat inbound (source='agent:{ava.self.AGENT_ID}')."""
        pin_agent(_spawn_agent())  # self identity

        child_id = ava.agents.spawn(prompt="\u53bb\u67e5 X")

        assert _inbound_rows(db_conn, child_id) == [
            ("\u53bb\u67e5 X", "chat", f"agent:{ava.self.AGENT_ID}"),
        ]

    def test_spawn_defaults_machine_to_local(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """When machine omitted, SDK defaults to local machine (ava.self.MACHINE_SPEC), gateway receives explicit target, no longer falls back to gateway's own machine."""
        from base.cluster.machine import machine_name

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
            idempotency_key: str | None = None,
        ) -> int:
            captured["machine"] = machine
            return 999

        monkeypatch.setattr(ava.gateway_client, "spawn", _fake_spawn)

        assert ava.agents.spawn() == 999
        assert captured["machine"] == machine_name()

    def test_spawn_explicit_machine_passthrough(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Explicit machine passthrough unchanged, not overridden by local default."""
        captured: dict[str, Any] = {}
        monkeypatch.setattr(ava.gateway_client, "spawn", lambda **kw: captured.update(kw) or 7)  # pyright: ignore[reportUnknownArgumentType]

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
        monkeypatch.setattr(gateway_client, "spawn", lambda **kw: seen.update(kw) or 3)  # pyright: ignore[reportUnknownArgumentType]

        assert agents.spawn(prompt=prompt) == 3  # pyright: ignore[reportArgumentType]
        assert seen["prompt"] == expected

    def test_spawn_rejects_non_string_prompt(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from ava import agents

        monkeypatch.setattr(gateway_client, "spawn", lambda **_kw: 3)  # pyright: ignore[reportUnknownArgumentType]

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

        monkeypatch.setattr(gateway_client, "spawn", lambda **_kw: 3)  # pyright: ignore[reportUnknownArgumentType]
        with pytest.raises(TypeError, match="prompt must be a string"):
            agents.spawn(prompt=prompt)  # pyright: ignore[reportArgumentType]


class TestSpawnFork:
    def test_fork_resolves_latest_checkpoint(self, db_conn: psycopg.Connection) -> None:
        """ava.agents.spawn(fork_from=N) internally resolves latest checkpoint
        (done by gateway, SDK unaware of ckpt id)."""
        pin_agent(_spawn_agent())  # self identity
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
        `raise_from_response` reverse lookup rebuild.
        """
        pin_agent(_spawn_agent())  # self identity
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
        pin_agent(_spawn_agent())  # self identity
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
        pin_agent(_spawn_agent())
        peer_id = ava.agents.spawn()

        result = ava.agents.terminate(peer_id, message="record the partial result")

        # Old-style comparisons keep working: the outcome reads as its status
        # string, and additionally carries the enum + the open-tasks hint.
        assert result == "enqueued"
        assert result == TerminateResult.ENQUEUED
        assert result.status is TerminateResult.ENQUEUED
        assert result.open_tasks is None
        assert _inbound_rows(db_conn, peer_id) == [
            ("record the partial result", "chat", f"agent:{ava.self.AGENT_ID}"),
            ("", "terminate", f"agent:{ava.self.AGENT_ID}"),
        ]

    def test_kill_all_shell_sessions_rides_the_sdk_body_end_to_end(
        self, db_conn: psycopg.Connection
    ) -> None:
        """On a live peer the graceful terminate records the kill for its exit."""
        pin_agent(_spawn_agent())
        peer_id = ava.agents.spawn()
        result = ava.agents.terminate(peer_id, kill_all_shell_sessions=True)
        assert result.shell_sessions == ava.agents.ShellSessionsKill(
            when=ShellSessionKillTiming.AT_EXIT, killed=[]
        )
        assert db_conn.execute(
            "SELECT payload FROM inbound_messages WHERE agent_id = %s AND kind = 'terminate'",
            (peer_id,),
        ).fetchone() == ({"kill_all_shell_sessions": True},)

    def test_terminate_reports_open_tasks_hint_with_truncation(
        self, db_conn: psycopg.Connection
    ) -> None:
        """`open_tasks` rides the SDK result: the agent's open tasks (newest
        first), truncated to five rows plus `more`; done/cancelled excluded."""
        pin_agent(_spawn_agent())
        peer_id = ava.agents.spawn()
        with db_conn.cursor() as cur:
            cur.execute(
                "INSERT INTO agent_tasks (title, description, status, created_by, owner) "
                "VALUES ('already finished', '', 'done', 'user', %s)",
                (peer_id,),
            )
            open_ids: list[int] = []
            for i in range(6):
                cur.execute(
                    "INSERT INTO agent_tasks (title, description, status, created_by, owner, updated_at) "
                    "VALUES (%s, '', 'in_progress', 'user', %s, now() - make_interval(mins => %s)) "
                    "RETURNING id",
                    (f"open task {i}", peer_id, i),
                )
                open_ids.append(cur.fetchone()[0])  # type: ignore[index]
        db_conn.commit()

        result = ava.agents.terminate(peer_id)

        assert result.status is TerminateResult.ENQUEUED
        hint = result.open_tasks
        assert hint is not None
        assert hint.count == 6
        assert len(hint.tasks) == 5
        assert hint.more == 1
        # Newest first; the closed task is not part of the hint.
        assert [task.id for task in hint.tasks] == open_ids[:5]
        assert all(task.status == "in_progress" for task in hint.tasks)
        assert all(isinstance(task.updated_at, datetime) for task in hint.tasks)
        assert str(hint).startswith("6 open task(s):")

    def test_already_terminated_keeps_old_style_compare(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The str-carrier contract holds for the already-dead result too."""

        def _terminate(*_args: object, **_kwargs: object) -> dict[str, Any]:
            return {"status": "already_terminated", "open_tasks": None}

        monkeypatch.setattr(gateway_client, "terminate", _terminate)
        result = ava.agents.terminate(7)
        assert result == "already_terminated"
        assert result == TerminateResult.ALREADY_TERMINATED
        assert result.status is TerminateResult.ALREADY_TERMINATED
        assert result.open_tasks is None
        # No kill was requested (and an older runner reports none): None.
        assert result.shell_sessions is None

    def test_terminate_reports_the_shell_session_kill(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _terminate(*_args: object, **kwargs: object) -> dict[str, Any]:
            assert kwargs["kill_all_shell_sessions"] is True
            shell = {"when": "now", "killed": [2, 5]}
            return {"status": "already_terminated", "open_tasks": None, "shell_sessions": shell}

        monkeypatch.setattr(gateway_client, "terminate", _terminate)
        result = ava.agents.terminate(7, kill_all_shell_sessions=True)
        assert result.shell_sessions == ava.agents.ShellSessionsKill(
            when=ShellSessionKillTiming.NOW, killed=[2, 5]
        )

    @pytest.mark.parametrize("when", ["later", "", None])
    def test_terminate_rejects_unknown_shell_cleanup_timing(
        self, monkeypatch: pytest.MonkeyPatch, when: object
    ) -> None:
        def _terminate(*_args: object, **_kwargs: object) -> dict[str, Any]:
            return {
                "status": "enqueued",
                "open_tasks": None,
                "shell_sessions": {"when": when, "killed": []},
            }

        monkeypatch.setattr(gateway_client, "terminate", _terminate)
        with pytest.raises(ValueError, match="ShellSessionKillTiming"):
            ava.agents.terminate(7, kill_all_shell_sessions=True)

    def test_rejects_non_string_message_before_gateway_call(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        called = False

        def _terminate(*_args: object, **_kwargs: object) -> dict[str, Any]:
            nonlocal called
            called = True
            return {"status": "enqueued", "open_tasks": None}

        monkeypatch.setattr(gateway_client, "terminate", _terminate)
        with pytest.raises(TypeError, match="message must be a string, got int"):
            ava.agents.terminate(7, message=42)  # pyright: ignore[reportArgumentType]
        assert not called


class TestSendMessage:
    def test_send_message_inserts_chat_inbound_with_agent_source(
        self, db_conn: psycopg.Connection
    ) -> None:
        """send_message purely INSERT inbound — no status check, no wait, no SendResult return."""
        pin_agent(_spawn_agent())
        peer_id = ava.agents.spawn()

        result = ava.agents.send_message(peer_id, "you got mail")
        assert result is None

        assert _inbound_rows(db_conn, peer_id) == [
            ("you got mail", "chat", f"agent:{ava.self.AGENT_ID}"),
        ]

    def test_send_message_to_terminated_is_fine(self, db_conn: psycopg.Connection) -> None:
        """send_message to terminated agent also INSERT inbound.
        SDK doesn't care about target state — purely send message, auto-resurrect is gateway-side detail."""
        pin_agent(_spawn_agent())
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
        pin_agent(_spawn_agent())
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
            gateway_client,
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

        monkeypatch.setattr(gateway_client, "send_message", lambda *_a, **_kw: None)  # pyright: ignore[reportUnknownArgumentType]
        with pytest.raises(TypeError, match="content must be a string"):
            agents.send_message(7, content)  # pyright: ignore[reportArgumentType]

    def test_send_message_content_blocks_pass_through(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A list of content-block dicts (multimodal path) is not string-joined."""
        from ava import agents

        seen: dict[str, Any] = {}
        monkeypatch.setattr(
            gateway_client,
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

        monkeypatch.setattr(gateway_client, "send_message", lambda *_a, **_kw: None)  # pyright: ignore[reportUnknownArgumentType]
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
        pin_agent(_spawn_agent())
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
        pin_agent(_spawn_agent())
        peer_id = ava.agents.spawn()

        inbound_id = ava.agents.send_system_note(
            peer_id,
            'Task #1 "t" is now assigned to you (by agent #1).',
            idempotency_key=str(uuid4()),
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
        assert payload == {"note_tag": "task", "delivery_resurrect": True}

    def test_send_system_note_preserves_explicit_task_id(self, db_conn: psycopg.Connection) -> None:
        pin_agent(_spawn_agent())
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
            peer_id,
            'Task #42 "t" is now assigned to you.',
            task_id=task_id,
            idempotency_key=str(uuid4()),
        )

        with db_conn.cursor() as cur:
            cur.execute("SELECT payload FROM inbound_messages WHERE agent_id = %s", (peer_id,))
            row = cur.fetchone()
        assert row is not None
        assert row[0] == {"note_tag": "task", "task_id": task_id, "delivery_resurrect": True}

    def test_send_system_note_to_terminated_is_fine(self, db_conn: psycopg.Connection) -> None:
        """A note with resurrect=True (task assignment) reaches a terminated
        agent — auto-resurrect is the gateway delivery detail, the SDK just
        posts the note and returns its id."""
        pin_agent(_spawn_agent())
        peer_id = ava.agents.spawn()
        with db_conn.cursor() as cur:
            cur.execute("UPDATE agents_meta SET status = 'terminated' WHERE id = %s", (peer_id,))
        db_conn.commit()

        inbound_id = ava.agents.send_system_note(
            peer_id, 'Task #1 "t" is now assigned to you.', idempotency_key=str(uuid4())
        )
        assert isinstance(inbound_id, int)
        with db_conn.cursor() as cur:
            cur.execute("SELECT kind FROM inbound_messages WHERE agent_id = %s", (peer_id,))
            kinds = [row[0] for row in cur.fetchall()]
        assert "system_note" in kinds

    def test_send_system_note_normalizes_tuple_content(self, db_conn: psycopg.Connection) -> None:
        """Same trailing-comma class as send_message — a one-element tuple
        unwraps to the note text instead of 422ing the gateway."""
        pin_agent(_spawn_agent())
        peer_id = ava.agents.spawn()

        # Runtime value of `("Task #1 is now " "assigned to you.",)`: implicit
        # concatenation plus trailing comma.
        content: object = ("Task #1 is now assigned to you.",)
        inbound_id = ava.agents.send_system_note(peer_id, content, idempotency_key=str(uuid4()))  # pyright: ignore[reportArgumentType]
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
        pin_agent(_spawn_agent())
        peer_id = ava.agents.spawn()

        content: object = ("Task #1 is now ", "assigned to you.")
        with pytest.raises(TypeError, match="content must be a string"):
            ava.agents.send_system_note(peer_id, content, idempotency_key=str(uuid4()))  # pyright: ignore[reportArgumentType]

    def test_send_system_note_to_nonexistent_raises(self, db_conn: psycopg.Connection) -> None:
        pin_agent(_spawn_agent())
        with pytest.raises(AgentNotFound):
            ava.agents.send_system_note(9999, "ghost", idempotency_key=str(uuid4()))
