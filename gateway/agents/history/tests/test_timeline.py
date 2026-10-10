"""Contract tests for GET /api/agents/{id}/timeline.

Design (2026-05-04 in-place simplification):
  - LangGraph state.messages is the sole source of truth
  - The inbound_messages table does **not** directly enter the timeline (it is already
    envelope-wrapped into LangGraph state), only used as a ts anchor
  - AIMessage.content (agent text) / tool_call code / reasoning are all rendered from
    state; adjacent timestamps within the same LLM turn no longer drift apart

Coverage:
  - endpoint basic contract (404 / empty agent / no orphaned inbounds displayed /
    lifecycle anchor filtering)
  - `_ai_message_items` helper unit (TestAiMessageItems): directly feed AIMessage to test
    block splitting
  - dispatch end-to-end (TestTimelineDispatch): truly load mixed state.messages into
    PostgresSaver checkpoint, run a full GET /timeline, verify that AIMessage / lifecycle /
    string-content paths all correctly render through the dispatch chain — unit test helpers
    passing ≠ dispatch routing correct (the bug in refactor 8a3c520 that accidentally deleted
    the elif header and silently lost AIMessage is exactly this kind of bug)
"""

import psycopg
import pytest
from fastapi.testclient import TestClient
from langchain_core.messages import BaseMessage, HumanMessage

from base.agents.history.timeline import (
    TimelineItem,
    build_timeline_items,
)
from base.db import create_agent
from gateway.app import app


@pytest.fixture
def test_client(db_conn: psycopg.Connection):
    """TestClient + lifespan. db_conn's TRUNCATE runs before the lifespan —
    the DB pool reuses the same test library (settings.data_plane.db_url is
    already replaced by conftest)."""
    with TestClient(app) as client:
        yield client


def test_timeline_404_for_unknown_thread(test_client: TestClient) -> None:
    resp = test_client.get("/api/agents/99999/timeline")
    assert resp.status_code == 404


def test_timeline_empty_for_new_agent(db_conn: psycopg.Connection, test_client: TestClient) -> None:
    """New agent with no LangGraph state → returns empty list."""
    tid = create_agent(db_conn)
    resp = test_client.get(f"/api/agents/{tid}/timeline")
    assert resp.status_code == 200
    assert resp.json() == {"items": [], "msg_count": 0, "has_more": False}


class TestSdkCallsProjection:
    """`agent_code.sdk_calls` is the runtime tally projected from the matching
    exec_output ToolMessage's metadata — never a scan of the code text, so a call
    that never executed cannot appear."""

    def test_agent_code_item_reads_the_exec_output_tally(self):
        from langchain_core.messages import AIMessage, ToolMessage

        ai = AIMessage(
            content="",
            tool_calls=[
                {"name": "execute_code", "args": {"code": "ava.files.read('x')"}, "id": "tc-1"}
            ],
        )
        out = ToolMessage(
            content="ok",
            tool_call_id="tc-1",
            additional_kwargs={
                "ava_msg_type": "exec_output",
                "sdk_calls": [{"method": "files.read", "count": 3}],
            },
        )
        items, _ = build_timeline_items([ai, out], [])
        code = next(it for it in items if it.kind == "agent_code")
        assert code.sdk_calls is not None
        assert [(c.method, c.count) for c in code.sdk_calls] == [("files.read", 3)]

    def test_code_without_exec_output_metadata_has_no_sdk_calls(self):
        """The AIMessage alone — its exec has not produced a ToolMessage yet —
        renders `sdk_calls=None` even though the code text calls ava.*: the
        projection trusts the metadata, not the text."""
        from langchain_core.messages import AIMessage

        ai = AIMessage(
            content="",
            tool_calls=[
                {"name": "execute_code", "args": {"code": "ava.files.read('x')"}, "id": "tc-1"}
            ],
        )
        items, _ = build_timeline_items([ai], [])
        code = next(it for it in items if it.kind == "agent_code")
        assert code.sdk_calls is None

    def test_empty_tally_is_a_real_zero_not_unknown(self):
        """`[]` (ran, called nothing) stays distinct from absent (unknown)."""
        from langchain_core.messages import AIMessage, ToolMessage

        ai = AIMessage(
            content="",
            tool_calls=[{"name": "execute_code", "args": {"code": "print(1)"}, "id": "tc-9"}],
        )
        out = ToolMessage(
            content="1",
            tool_call_id="tc-9",
            additional_kwargs={"ava_msg_type": "exec_output", "sdk_calls": []},
        )
        items, _ = build_timeline_items([ai, out], [])
        code = next(it for it in items if it.kind == "agent_code")
        assert code.sdk_calls == []

    def test_metadata_pairs_by_tool_call_id_not_message_order(self):
        """Two code blocks in one render each get their own exec_output's counts."""
        from langchain_core.messages import AIMessage, ToolMessage

        ai = AIMessage(
            content="",
            tool_calls=[
                {"name": "execute_code", "args": {"code": "first()"}, "id": "tc-a"},
                {"name": "execute_code", "args": {"code": "second()"}, "id": "tc-b"},
            ],
        )
        out_a = ToolMessage(
            content="a",
            tool_call_id="tc-a",
            additional_kwargs={
                "ava_msg_type": "exec_output",
                "sdk_calls": [{"method": "a.fn", "count": 1}],
            },
        )
        out_b = ToolMessage(
            content="b",
            tool_call_id="tc-b",
            additional_kwargs={
                "ava_msg_type": "exec_output",
                "sdk_calls": [{"method": "b.fn", "count": 2}],
            },
        )
        items, _ = build_timeline_items([ai, out_a, out_b], [])
        codes = [it for it in items if it.kind == "agent_code"]
        assert [[(c.method, c.count) for c in (it.sdk_calls or [])] for it in codes] == [
            [("a.fn", 1)],
            [("b.fn", 2)],
        ]


class TestAvaMsgTypeDispatch:
    """Every AvaMsgType member must dispatch to its intended timeline item kind.

    A HumanMessage tagged with a known ava_msg_type that the render loop has no
    branch for falls to `_fallback_human_item` — a system_marker with
    source=None that the frontend renders as the red "unrecognized" alarm
    (the #1017 regression class: the auto-compact path produced an untagged
    summary). This parametrized test makes "add a new AvaMsgType without
    adapting the renderer" impossible: every member must render as its
    intended kind, never the null-source catch-all.

    Companion cross-stack contract: tests/contracts/test_lint_marker_contract.py
    (backend NoteTag ⊆ frontend dispatch sets) + the frontend marker-contract
    tests in ui/web/src/components/timeline/timeline.test.tsx.
    """

    @staticmethod
    def _render(msg: HumanMessage) -> list[TimelineItem]:

        return build_timeline_items([msg], [])[0]

    @staticmethod
    def _tagged(msg_type: str, **extra: object) -> HumanMessage:
        return HumanMessage(
            content="payload",
            additional_kwargs={"ava_msg_type": msg_type, **extra},
        )

    def test_every_msg_type_has_an_explicit_branch(self) -> None:
        from base.agents.messages.kwargs import AvaMsgType

        expected_kind = {
            AvaMsgType.INBOUND: "inbound_chat",
            AvaMsgType.EXEC_OUTPUT: "code_output",
            AvaMsgType.SYSTEM_NOTE: "system_marker",
            AvaMsgType.COMPACT_SUMMARY: "inbound_compact_summary",
            AvaMsgType.COMPACT_REQUEST: "inbound_compact_request",
        }
        # SYSTEM_NOTE needs a note_tag to render as its card chip (not the
        # null-source historical-data alarm path).
        note_tag = {"ava_note_tag": "sdk_hint"}
        by_type = {AvaMsgType.SYSTEM_NOTE: note_tag}
        for msg_type, expected in expected_kind.items():
            items = self._render(self._tagged(msg_type.value, **by_type.get(msg_type, {})))
            assert len(items) == 1, f"{msg_type}: expected exactly one item, got {items}"
            item = items[0]
            assert item.kind == expected, (
                f"AvaMsgType.{msg_type.name} renders as {item.kind!r} (source={item.source!r}), "
                f"expected {expected!r} — the render loop needs an explicit branch for it"
            )
            # A tagged message must NEVER hit the catch-all (system_marker +
            # source=None). SYSTEM_NOTE with a tag is the card path; without a
            # tag it is deliberately fail-loud historical data, so only assert
            # source on the tagged SYSTEM_NOTE construction.
            if item.kind == "system_marker":
                assert item.source is not None, (
                    f"AvaMsgType.{msg_type.name} fell to the null-source catch-all: {item}"
                )

    def test_untagged_human_message_hits_catch_all_fail_loud(self) -> None:
        """Untagged framework messages DO hit the catch-all — that is the
        fail-loud design (a red chip beats silent mis-rendering). Compact
        paths are asserted by the agent-side tests to never produce one."""
        items = self._render(HumanMessage(content="legacy untagged note"))
        assert len(items) == 1
        assert items[0].kind == "system_marker"
        assert items[0].source is None

    def test_compact_summary_item_carries_the_compact_run_id(self) -> None:
        """Task #3323: the ava_compact_id durable anchor rides into the
        timeline item — the frontend matches the live ticking block to this
        summary by it. Pre-anchor summaries render with None."""
        from base.agents.messages.kwargs import AvaMsgType

        anchored = self._render(
            self._tagged(AvaMsgType.COMPACT_REQUEST.value, ava_compact_id="run-1")
        )
        assert len(anchored) == 1
        assert anchored[0].kind == "inbound_compact_request"
        assert anchored[0].compact_id == "run-1"
        unanchored = self._render(self._tagged(AvaMsgType.COMPACT_SUMMARY.value))
        assert unanchored[0].compact_id is None

        # Side effect: get_timeline's _log should have had a warning trace (operators can grep)
        # Do not assert log content — avoid coupling with logger implementation details


def _items(*ids: str) -> list[TimelineItem]:
    return [TimelineItem(item_id=i, kind="agent_chat", payload=i) for i in ids]


class CompactHistoryCases:
    @staticmethod
    def _summary(content: str) -> HumanMessage:
        return HumanMessage(
            content=content,
            additional_kwargs={
                "ava_msg_type": "compact_summary",
                "ava_created_at": "2026-08-25T00:00:00+00:00",
            },
        )

    @classmethod
    def _segment(cls, name: str, item_count: int) -> list[BaseMessage]:
        from langchain_core.messages import AIMessage, SystemMessage

        return [
            SystemMessage(content="system"),
            cls._summary(f"{name} summary"),
            *(AIMessage(content=f"{name} item {i}") for i in range(item_count)),
        ]

    @classmethod
    def _current(cls, *messages: BaseMessage) -> list[BaseMessage]:
        from langchain_core.messages import SystemMessage

        return [SystemMessage(content="system"), cls._summary("current summary"), *messages]

    @staticmethod
    def _put_checkpoint(
        agent_id: int,
        messages: list[BaseMessage],
        *,
        version: str,
        boundary: bool = False,
    ) -> str:
        from typing import cast

        from langgraph.checkpoint.base import CheckpointMetadata, empty_checkpoint
        from langgraph.checkpoint.postgres import PostgresSaver

        from base.config import settings

        checkpoint = empty_checkpoint()
        checkpoint["channel_values"] = {"messages": messages}
        checkpoint["channel_versions"] = {"messages": version, "__start__": "1"}
        metadata: dict[str, object] = {"source": "input", "step": int(version), "parents": {}}
        if boundary:
            metadata["compact_boundary"] = True
        with PostgresSaver.from_conn_string(settings.data_plane.db_url) as saver:
            saver.setup()
            saved = saver.put(
                config={"configurable": {"thread_id": str(agent_id), "checkpoint_ns": ""}},
                checkpoint=checkpoint,
                metadata=cast(CheckpointMetadata, metadata),
                new_versions={"messages": version},
            )
        return str((saved.get("configurable") or {})["checkpoint_id"])


class TestTimelineFailLoud:
    """endpoint error handling boundaries: IO errors return empty list 200 (debug log),
    dispatch errors raise to 500 fail-loud.

    Historical lesson (8a3c520 → fixed in #50): during the refactor, a missing import +
    missing elif branch header caused NameError to be swallowed by the outer except Exception,
    the timeline silently truncated and users saw garbled order instead of a 500. This class
    prevents regression: any logic error in the dispatch layer must propagate as a 500.
    """

    @staticmethod
    def _put_minimal_aimessage(agent_id: int) -> None:
        """Load a single AIMessage so dispatch hits the _ai_message_items path.
        Tests replace the public renderer, triggering a RuntimeError to verify
        the endpoint's fail-loud boundary."""
        from langchain_core.messages import AIMessage
        from langgraph.checkpoint.base import empty_checkpoint
        from langgraph.checkpoint.postgres import PostgresSaver

        from base.config import settings

        ckpt = empty_checkpoint()
        ckpt["channel_values"] = {"messages": [AIMessage(content="hello")]}
        ckpt["channel_versions"] = {"messages": "1", "__start__": "1"}
        with PostgresSaver.from_conn_string(settings.data_plane.db_url) as saver:
            saver.setup()
            saver.put(
                config={
                    "configurable": {
                        "thread_id": str(agent_id),
                        "checkpoint_ns": "",
                    }
                },
                checkpoint=ckpt,
                metadata={"source": "input", "step": 1, "parents": {}},
                new_versions={"messages": "1"},
            )

    def test_dispatch_error_propagates_as_500(
        self,
        db_conn: psycopg.Connection,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Any logic error in the dispatch layer (NameError / KeyError / custom helper raise)
        must propagate as endpoint 500 — must not be swallowed into 200 + empty list.

        Bug reproduce: replace the public build_timeline_items renderer with a raise RuntimeError, feed one
        AIMessage into state. Old code (try wrapped the entire loop): endpoint 200 +
        empty list + debug log swallowed. New code (try only wraps saver IO): 500.

        Note raise_app_exceptions=False: by default TestClient re-raises endpoint exceptions
        to the test assertion layer (convenient for debugging), but this test needs to see
        the HTTP-layer 500 status, so disable re-raise to let FastAPI run the default error
        middleware and return 500.
        """
        from gateway.agents.history import timeline as gateway_timeline

        tid = create_agent(db_conn)
        self._put_minimal_aimessage(tid)

        def boom(*_args, **_kwargs):
            raise RuntimeError(
                "simulated dispatch failure (e.g. NameError when refactor forgot import)"
            )

        monkeypatch.setattr(gateway_timeline, "build_timeline_items", boom)  # pyright: ignore[reportUnknownArgumentType]
        with TestClient(app, raise_server_exceptions=False) as client:
            resp = client.get(f"/api/agents/{tid}/timeline")
        assert resp.status_code == 500, (
            f"dispatch error must fail-loud as 500, got {resp.status_code}: {resp.text[:200]}"
        )

    def test_io_error_returns_empty_list_with_200(
        self,
        db_conn: psycopg.Connection,
        test_client: TestClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """IO layer errors (DB connection drop / deserialization failure) still follow the
        current contract returning empty list 200 — timeline is a cold-load view, when
        checkpoint read fails, don't block the UI (frontend gets no timeline but can continue
        rendering other pages).

        Path: load_checkpoint_messages raises IO failures as CheckpointReadError,
        load_current_messages catches it → warning log + messages=[]. Prevents the fail-loud
        refactor from going too far and turning read failures into 500 affecting UX
        (contrast with the /messages data endpoint 503: that doesn't tolerate, this one does).
        """

        tid = create_agent(db_conn)
        attempts: list[object] = []
        # Fail at the adapter's tuple read to simulate a database disruption.
        from typing import NoReturn

        def fail_read(_saver: object, config: object) -> NoReturn:
            attempts.append(config)
            raise OSError("simulated DB connection lost")

        # load_checkpoint_messages imports the adapter inside the function.
        import base.agents.history.checkpoint_postgres_walks as ckpt_mod

        monkeypatch.setattr(ckpt_mod.HistoryPostgresSaver, "get_tuple", fail_read)
        resp = test_client.get(f"/api/agents/{tid}/timeline")
        assert resp.status_code == 200
        assert resp.json() == {"items": [], "msg_count": 0, "has_more": False}
        assert len(attempts) == 1
