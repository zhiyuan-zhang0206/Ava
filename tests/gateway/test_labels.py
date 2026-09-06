"""Tests for thread label auto-generation + PATCH endpoint.

`generate_label_async` monkeypatches `build_chat_model` to use a fake LLM,
avoiding real DeepSeek calls. Covers CAS / fail-soft on failure / publish event.
"""

from __future__ import annotations

from typing import Any

import psycopg
import pytest
import redis.asyncio as aredis
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage

import services.labeler.labeler as labels_module
from gateway.app import app
from services.labeler.labeler import _normalize, generate_label_async
from shared.config import settings
from shared.db import create_agent
from shared.labels import publish_label_updated


def _label_of(conn: psycopg.Connection, agent_id: int) -> str | None:
    with conn.cursor() as cur:
        cur.execute("SELECT label FROM agents WHERE id = %s", (agent_id,))
        row = cur.fetchone()
        assert row is not None
        return row[0]


def _label_user_set(conn: psycopg.Connection, agent_id: int) -> bool:
    with conn.cursor() as cur:
        cur.execute("SELECT label_user_set FROM agents WHERE id = %s", (agent_id,))
        row = cur.fetchone()
        assert row is not None
        return row[0]


class _FakeLLM:
    """ainvoke returns a fixed content, simulating LLM invocation."""

    def __init__(self, content: str) -> None:
        self.content = content
        self.calls: list[Any] = []

    async def ainvoke(self, messages: list[Any]) -> AIMessage:
        self.calls.append(messages)
        return AIMessage(content=self.content)


class _FakeLLMWithThinking:
    """ainvoke returns list content (simulating Anthropic thinking blocks).

    ChatAnthropic with extended thinking returns content as list:
    [{"type":"thinking","thinking":"...","signature":"sig-..."},
     {"type":"text","text":"label text"}]
    str() on this dumps signature into label -- the bug being fixed.
    """

    def __init__(self, text_label: str) -> None:
        self.text_label = text_label

    async def ainvoke(self, _messages: list[Any]) -> AIMessage:
        return AIMessage(
            content=[
                {
                    "type": "thinking",
                    "thinking": "thinking...",
                    "signature": "sig-abc123",
                    "index": 0,
                },
                {"type": "text", "text": self.text_label},
            ]
        )


class TestNormalize:
    def test_strip_quotes_and_truncate(self) -> None:
        assert _normalize('  "abc"  ') == "abc"

    def test_chinese_quote_brackets(self) -> None:
        assert _normalize("\u300c\u6d4b\u8bd5\u4efb\u52a1\u300d") == "\u6d4b\u8bd5\u4efb\u52a1"

    def test_truncate_at_max_chars(self) -> None:
        # Within 64 chars — no truncation
        assert (
            _normalize("\u4e00\u4e8c\u4e09\u56db\u4e94\u516d\u4e03\u516b\u4e5d\u5341")
            == "\u4e00\u4e8c\u4e09\u56db\u4e94\u516d\u4e03\u516b\u4e5d\u5341"
        )

    def test_first_line_only(self) -> None:
        assert _normalize("first line\nsecond line") == "first line"

    def test_empty(self) -> None:
        assert _normalize("   ") == ""


@pytest.mark.asyncio
async def test_labeler_emits_batch_billing_after_a_successful_llm_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A label response is accounted as a batch call before label validation.

    The regression this catches is a provider call that returns an unusable
    label being absent from the billing ledger despite consuming tokens.
    """
    emitted: list[tuple[AIMessage, dict[str, object]]] = []
    response = AIMessage(
        content="",
        usage_metadata={"input_tokens": 10, "output_tokens": 2, "total_tokens": 12},
    )

    class _ResponseLLM:
        async def ainvoke(self, _messages: list[Any]) -> AIMessage:
            return response

    def _emit(
        message: AIMessage,
        model: str,
        *,
        usage_kind: str,
        for_agent_id: int | None = None,
    ) -> None:
        emitted.append(
            (
                message,
                {
                    "model": model,
                    "usage_kind": usage_kind,
                    "for_agent_id": for_agent_id,
                },
            )
        )

    monkeypatch.setattr(
        labels_module,
        "build_chat_model",
        lambda _model, **_kwargs: _ResponseLLM(),  # pyright: ignore[reportUnknownArgumentType]
    )
    monkeypatch.setattr("shared.lm.usage.log_usage_from_message", _emit)

    assert await generate_label_async(1, "prompt", "deepseek-v4-pro") is False
    assert emitted == [
        (
            response,
            {
                "model": "deepseek-v4-pro",
                "usage_kind": "batch",
                "for_agent_id": 1,
            },
        )
    ]


class TestGenerateLabelAsync:
    @pytest.mark.asyncio
    async def test_writes_label_when_null_and_publishes(
        self, db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        tid = create_agent(db_conn)  # label NULL + label_user_set FALSE by default
        monkeypatch.setattr(
            labels_module,
            "build_chat_model",
            lambda _m, **_: _FakeLLM("\u67e5 X \u6a21\u5757"),  # pyright: ignore[reportUnknownArgumentType]
        )
        published: list[str] = []

        async def _capture(_self: Any, channel: str, payload: str) -> int:
            assert channel == settings.data_plane.events_channel
            published.append(payload)
            return 1

        # patch publish path — don't hit real redis
        monkeypatch.setattr(aredis.Redis, "publish", _capture, raising=False)

        await generate_label_async(
            tid, "\u67e5\u4e00\u4e0b X \u6a21\u5757\u600e\u4e48\u8c03", "deepseek-v4-pro"
        )
        assert _label_of(db_conn, tid) == "\u67e5 X \u6a21\u5757"
        # LLM write does not flip sticky bit — user can still PATCH rename (LLM-written label counts as "not yet user-touched")
        # only user PATCH flips it
        assert _label_user_set(db_conn, tid) is False
        assert len(published) == 1
        assert '"label_updated"' in published[0]
        assert '"label":"\u67e5 X \u6a21\u5757"' in published[0]

    @pytest.mark.asyncio
    async def test_cas_skips_when_label_already_set(
        self, db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When the user has already written a label via PATCH → LLM result should not overwrite."""
        tid = create_agent(db_conn)
        with db_conn.cursor() as cur:
            cur.execute(
                "UPDATE agents SET label='\u7528\u6237\u6539\u7684', label_user_set=TRUE WHERE id=%s",
                (tid,),
            )
        db_conn.commit()
        monkeypatch.setattr(
            labels_module,
            "build_chat_model",
            lambda _m, **_: _FakeLLM("LLM \u8d77\u7684"),  # pyright: ignore[reportUnknownArgumentType]
        )
        published: list[str] = []

        async def _capture(_self: Any, channel: str, payload: str) -> int:
            published.append(payload)
            return 1

        monkeypatch.setattr(aredis.Redis, "publish", _capture, raising=False)

        await generate_label_async(tid, "\u539f\u59cb prompt", "deepseek-v4-pro")
        assert _label_of(db_conn, tid) == "\u7528\u6237\u6539\u7684"
        # CAS miss → do not publish
        assert published == []

    @pytest.mark.asyncio
    async def test_cas_skips_after_user_reset(
        self, db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """After user PATCH reset writes label back to NULL + label_user_set=TRUE, the LLM
        result should not hit again (blocked by `AND NOT label_user_set`). This is a critical race —
        without sticky bit the reset semantics would be broken."""
        tid = create_agent(db_conn)
        # simulate user PATCH reset (empty string goes through reset branch): write NULL while label_user_set=TRUE
        with db_conn.cursor() as cur:
            cur.execute(
                "UPDATE agents SET label=NULL, label_user_set=TRUE WHERE id=%s",
                (tid,),
            )
        db_conn.commit()
        monkeypatch.setattr(
            labels_module,
            "build_chat_model",
            lambda _m, **_: _FakeLLM("LLM \u8d77\u7684"),  # pyright: ignore[reportUnknownArgumentType]
        )
        published: list[str] = []

        async def _capture(_self: Any, channel: str, payload: str) -> int:
            published.append(payload)
            return 1

        monkeypatch.setattr(aredis.Redis, "publish", _capture, raising=False)

        await generate_label_async(tid, "\u539f\u59cb prompt", "deepseek-v4-pro")
        assert _label_of(db_conn, tid) is None
        assert published == []

    @pytest.mark.asyncio
    async def test_llm_failure_leaves_label_null(
        self, db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        tid = create_agent(db_conn)

        class _ExplodingLLM:
            async def ainvoke(self, _messages: Any) -> Any:
                raise RuntimeError("api boom")

        monkeypatch.setattr(labels_module, "build_chat_model", lambda _m, **_: _ExplodingLLM())  # pyright: ignore[reportUnknownArgumentType]
        published: list[str] = []

        async def _capture(_self: Any, channel: str, payload: str) -> int:
            published.append(payload)
            return 1

        monkeypatch.setattr(aredis.Redis, "publish", _capture, raising=False)

        # fail-soft: does not raise
        await generate_label_async(tid, "p", "deepseek-v4-pro")
        assert _label_of(db_conn, tid) is None
        assert published == []

    @pytest.mark.asyncio
    async def test_empty_normalized_label_skipped(
        self, db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """LLM returns blank / only quotes → _normalize returns "" → do not write to DB, do not publish."""
        tid = create_agent(db_conn)
        monkeypatch.setattr(labels_module, "build_chat_model", lambda _m, **_: _FakeLLM('"   "'))  # pyright: ignore[reportUnknownArgumentType]
        published: list[str] = []

        async def _capture(_self: Any, channel: str, payload: str) -> int:
            published.append(payload)
            return 1

        monkeypatch.setattr(aredis.Redis, "publish", _capture, raising=False)

        await generate_label_async(tid, "p", "deepseek-v4-pro")
        assert _label_of(db_conn, tid) is None
        assert published == []

    @pytest.mark.asyncio
    async def test_extracts_text_from_thinking_blocks(
        self, db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """content is list [thinking, text] -> only text block extracted, signature NOT in label."""
        tid = create_agent(db_conn)
        monkeypatch.setattr(
            labels_module,
            "build_chat_model",
            lambda _m, **_: _FakeLLMWithThinking("migrate data"),  # pyright: ignore[reportUnknownArgumentType]
        )
        published: list[str] = []

        async def _capture(_self: Any, _channel: str, payload: str) -> int:
            published.append(payload)
            return 1

        monkeypatch.setattr(aredis.Redis, "publish", _capture, raising=False)

        await generate_label_async(tid, "migrate database schema", "deepseek-v4-pro")
        assert _label_of(db_conn, tid) == "migrate data"
        assert '"label":"migrate data"' in published[0]

    @pytest.mark.asyncio
    async def test_thinking_only_blocks_yield_empty(
        self, db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """content is all thinking blocks with no text -> raw="" -> skip, label stays NULL.

        Edge case: LLM started extended thinking but stopped before generating text
        (e.g. max_tokens truncated during thinking). Don't crash, don't dump signature.
        """
        tid = create_agent(db_conn)

        class _ThinkingOnlyLLM:
            async def ainvoke(self, _messages: Any) -> AIMessage:
                return AIMessage(
                    content=[
                        {
                            "type": "thinking",
                            "thinking": "analyzing...",
                            "signature": "sig-xyz",
                            "index": 0,
                        },
                    ]
                )

        monkeypatch.setattr(labels_module, "build_chat_model", lambda _m, **_: _ThinkingOnlyLLM())  # pyright: ignore[reportUnknownArgumentType]
        published: list[str] = []

        async def _capture(_self: Any, _channel: str, payload: str) -> int:
            published.append(payload)
            return 1

        monkeypatch.setattr(aredis.Redis, "publish", _capture, raising=False)

        await generate_label_async(tid, "p", "deepseek-v4-pro")
        assert _label_of(db_conn, tid) is None
        assert published == []

    @pytest.mark.asyncio
    async def test_multiple_text_blocks_joined(
        self, db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Multiple text blocks -> joined with space.

        Anthropic protocol allows multiple text blocks interleaved with
        thinking/tool_use. In practice rare for label gen but don't crash.
        """
        tid = create_agent(db_conn)

        class _MultiTextLLM:
            async def ainvoke(self, _messages: Any) -> AIMessage:
                return AIMessage(
                    content=[
                        {"type": "text", "text": "migrate"},
                        {"type": "thinking", "thinking": "hmm", "signature": "sig", "index": 0},
                        {"type": "text", "text": "data"},
                    ]
                )

        monkeypatch.setattr(labels_module, "build_chat_model", lambda _m, **_: _MultiTextLLM())  # pyright: ignore[reportUnknownArgumentType]
        published: list[str] = []

        async def _capture(_self: Any, _channel: str, payload: str) -> int:
            published.append(payload)
            return 1

        monkeypatch.setattr(aredis.Redis, "publish", _capture, raising=False)

        await generate_label_async(tid, "p", "deepseek-v4-pro")
        assert _label_of(db_conn, tid) == "migrate data"


class TestPatchThread:
    def test_patch_writes_label_and_flips_user_set(self, db_conn: psycopg.Connection) -> None:
        tid = create_agent(db_conn)
        assert _label_user_set(db_conn, tid) is False
        with TestClient(app) as client:
            resp = client.patch(
                f"/api/agents/{tid}", json={"label": "\u6211\u81ea\u5df1\u8d77\u7684\u540d"}
            )
        assert resp.status_code == 204
        assert _label_of(db_conn, tid) == "\u6211\u81ea\u5df1\u8d77\u7684\u540d"
        assert _label_user_set(db_conn, tid) is True

    def test_patch_reset_also_flips_user_set(self, db_conn: psycopg.Connection) -> None:
        """Empty string reset is also an active user operation — sticky bit is also flipped TRUE, LLM can no longer overwrite."""
        tid = create_agent(db_conn)
        with db_conn.cursor() as cur:
            cur.execute("UPDATE agents SET label='\u4e34\u65f6' WHERE id=%s", (tid,))
        db_conn.commit()
        with TestClient(app) as client:
            resp = client.patch(f"/api/agents/{tid}", json={"label": ""})
        assert resp.status_code == 204
        assert _label_of(db_conn, tid) is None
        assert _label_user_set(db_conn, tid) is True

    def test_patch_empty_resets_to_null(self, db_conn: psycopg.Connection) -> None:
        tid = create_agent(db_conn)
        with db_conn.cursor() as cur:
            cur.execute("UPDATE agents SET label='\u4e34\u65f6' WHERE id=%s", (tid,))
        db_conn.commit()
        with TestClient(app) as client:
            resp = client.patch(f"/api/agents/{tid}", json={"label": ""})
        assert resp.status_code == 204
        assert _label_of(db_conn, tid) is None

    def test_patch_strips_whitespace(self, db_conn: psycopg.Connection) -> None:
        tid = create_agent(db_conn)
        with TestClient(app) as client:
            resp = client.patch(f"/api/agents/{tid}", json={"label": "   x   "})
        assert resp.status_code == 204
        # actually written as stripped "x"
        assert _label_of(db_conn, tid) == "x"

    def test_patch_only_whitespace_resets_to_null(self, db_conn: psycopg.Connection) -> None:
        """All whitespace after strip becomes "" → goes through reset branch."""
        tid = create_agent(db_conn)
        with db_conn.cursor() as cur:
            cur.execute("UPDATE agents SET label='something' WHERE id=%s", (tid,))
        db_conn.commit()
        with TestClient(app) as client:
            resp = client.patch(f"/api/agents/{tid}", json={"label": "    "})
        assert resp.status_code == 204
        assert _label_of(db_conn, tid) is None

    def test_patch_404_when_thread_missing(self, db_conn: psycopg.Connection) -> None:
        with TestClient(app) as client:
            resp = client.patch("/api/agents/9999", json={"label": "x"})
        assert resp.status_code == 404

    def test_patch_too_long_label_422(self, db_conn: psycopg.Connection) -> None:
        tid = create_agent(db_conn)
        long_label = "x" * 100  # > 64
        with TestClient(app) as client:
            resp = client.patch(f"/api/agents/{tid}", json={"label": long_label})
        assert resp.status_code == 422


class TestSpawnAgentSchedulesLabelGeneration:
    def test_spawn_with_prompt_leaves_label_null(
        self, db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """POST /api/agents with prompt → label remains NULL.

        Label auto-generation has been moved from the Gateway BackgroundTask to a separate services/labeler daemon.
        The Gateway spawn endpoint no longer triggers label generation — the labeler daemon asynchronously polls
        the agents table and discovers new agents to generate labels."""
        monkeypatch.setattr(
            labels_module,
            "build_chat_model",
            lambda _m, **_: _FakeLLM("\u8fc1\u79fb\u6570\u636e"),  # pyright: ignore[reportUnknownArgumentType]
        )

        async def _noop_publish(_self: Any, channel: str, payload: str) -> int:
            return 1

        monkeypatch.setattr(aredis.Redis, "publish", _noop_publish, raising=False)

        with TestClient(app) as client:
            resp = client.post(
                "/api/agents",
                json={"prompt": "\u8fc1\u79fb\u6570\u636e\u5e93 schema", "prompt_source": "user"},
            )
        assert resp.status_code == 201
        new_id = resp.json()["id"]
        assert _label_of(db_conn, new_id) is None

    def test_spawn_without_prompt_leaves_label_null(
        self, db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Spawn without prompt → does not run LLM, label stays NULL."""

        # any build_chat_model call is considered a failure (confirm the task was not triggered)
        def _no_llm(_m: str, **_: Any) -> Any:
            raise AssertionError("LLM should not be invoked when prompt is None")

        monkeypatch.setattr(labels_module, "build_chat_model", _no_llm)

        with TestClient(app) as client:
            resp = client.post("/api/agents", json={})
        assert resp.status_code == 201
        new_id = resp.json()["id"]
        assert _label_of(db_conn, new_id) is None


class TestPublishLabelUpdated:
    @pytest.mark.asyncio
    async def test_publish_label_updated_uses_settings_channel(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured: dict[str, str] = {}

        async def _capture(_self: Any, channel: str, payload: str) -> int:
            captured["channel"] = channel
            captured["payload"] = payload
            return 1

        monkeypatch.setattr(aredis.Redis, "publish", _capture, raising=False)

        await publish_label_updated(42, "\u77ed\u540d")
        assert captured["channel"] == settings.data_plane.events_channel
        assert '"agent_id":42' in captured["payload"]
        assert '"label":"\u77ed\u540d"' in captured["payload"]
