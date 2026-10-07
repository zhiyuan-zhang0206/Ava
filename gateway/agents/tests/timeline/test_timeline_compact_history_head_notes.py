"""Timeline cases: timeline compact history head notes."""

from __future__ import annotations

import psycopg
import pytest
from fastapi.testclient import TestClient
from langchain_core.messages import BaseMessage, HumanMessage

from base.db import create_agent
from gateway.agents.tests.test_timeline import CompactHistoryCases
from gateway.agents.tests.test_timeline import test_client as test_client


class TestTimelineCompactHistoryHeadNotes(CompactHistoryCases):
    @pytest.mark.parametrize(
        "before",
        [
            f"{'1' * 5000}.0",
            f"s1.boundary.1.{'0' * 5000}",
        ],
    )
    def test_oversized_numeric_cursor_is_terminal_not_500(
        self,
        db_conn: psycopg.Connection,
        test_client: TestClient,
        monkeypatch: pytest.MonkeyPatch,
        before: str,
    ) -> None:
        from langchain_core.messages import AIMessage

        from base.config import settings

        tid = create_agent(db_conn)
        self._put_checkpoint(
            tid,
            self._current(AIMessage(content="current item")),
            version="1",
        )
        monkeypatch.setattr(settings.gateway, "timeline_compact_history", -1)

        response = test_client.get(
            f"/api/agents/{tid}/timeline",
            params={"before": before, "limit": 50},
        )

        assert response.status_code == 200
        assert response.json() == {"items": [], "msg_count": 3, "has_more": False}

    def test_missing_cross_segment_target_is_terminal_even_if_an_older_boundary_exists(
        self,
        db_conn: psycopg.Connection,
        test_client: TestClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from langchain_core.messages import AIMessage

        import gateway.agents.timeline as timeline_router
        from base.config import settings

        tid = create_agent(db_conn)
        self._put_checkpoint(
            tid,
            self._current(AIMessage(content="current item")),
            version="1",
        )
        monkeypatch.setattr(settings.gateway, "timeline_compact_history", -1)

        def boundary_ids(_db: object, _agent_id: int, *, limit: int | None = None) -> list[str]:
            del limit
            return ["missing-newer", "still-older"]

        def missing_segment(_db: object, _agent_id: int, _checkpoint_id: str) -> list[BaseMessage]:
            return []

        monkeypatch.setattr(timeline_router, "list_compact_boundary_checkpoint_ids", boundary_ids)
        monkeypatch.setattr(timeline_router, "load_checkpoint_messages_segment", missing_segment)

        page = test_client.get(
            f"/api/agents/{tid}/timeline",
            params={"before": "2.0", "limit": 50},
        )

        assert page.status_code == 200
        assert [item["item_id"] for item in page.json()["items"]] == ["0.0", "1.0"]
        assert page.json()["msg_count"] == 3
        assert page.json()["has_more"] is False

    def test_standing_note_cursor_crosses_from_current_segment(
        self,
        db_conn: psycopg.Connection,
        test_client: TestClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from langchain_core.messages import AIMessage, HumanMessage

        from base.config import settings

        tid = create_agent(db_conn)
        boundary_id = self._put_checkpoint(
            tid, self._segment("history", 1), version="1", boundary=True
        )
        standing_note = HumanMessage(
            content="remember this",
            additional_kwargs={
                "ava_msg_type": "system_note",
                "ava_note_tag": "memory",
                "ava_created_at": "2026-08-25T00:00:01+00:00",
            },
        )
        self._put_checkpoint(
            tid,
            self._current(standing_note, AIMessage(content="current item")),
            version="2",
        )
        monkeypatch.setattr(settings.gateway, "timeline_compact_history", 1)

        page = test_client.get(
            f"/api/agents/{tid}/timeline", params={"before": "2.0", "limit": 50}
        ).json()

        assert [item["item_id"] for item in page["items"]] == [
            "0.0",
            "1.0",
            f"s1.{boundary_id}.0.0",
            f"s1.{boundary_id}.1.0",
        ]
        assert page["has_more"] is False

    @pytest.mark.parametrize("damage", ["read_error", "missing", "malformed_message"])
    def test_damaged_or_disappeared_segment_returns_terminal_empty_window(
        self,
        db_conn: psycopg.Connection,
        test_client: TestClient,
        monkeypatch: pytest.MonkeyPatch,
        damage: str,
    ) -> None:
        from langchain_core.messages import AIMessage

        import gateway.agents.timeline as timeline_router
        from base.agents.history.checkpoint import CheckpointReadError
        from base.config import settings

        tid = create_agent(db_conn)
        self._put_checkpoint(
            tid,
            self._current(AIMessage(content="current item")),
            version="1",
        )
        checkpoint_id = "1f0b9b12-0000-6000-8000-000000000000"
        monkeypatch.setattr(settings.gateway, "timeline_compact_history", -1)

        def boundary_ids(_db: object, _agent_id: int, *, limit: int | None = None) -> list[str]:
            del limit
            return [checkpoint_id]

        monkeypatch.setattr(timeline_router, "list_compact_boundary_checkpoint_ids", boundary_ids)
        if damage == "missing":

            def missing_segment(*_args: object) -> list[BaseMessage]:
                return []

            monkeypatch.setattr(
                timeline_router,
                "load_checkpoint_messages_segment",
                missing_segment,
            )
        elif damage == "read_error":

            def fail_read(_db: object, _agent_id: int, _checkpoint_id: str) -> list[BaseMessage]:
                raise CheckpointReadError("damaged boundary")

            monkeypatch.setattr(timeline_router, "load_checkpoint_messages_segment", fail_read)
        else:

            def malformed_segment(*_args: object) -> list[BaseMessage]:
                return [
                    HumanMessage(
                        content="damaged inbound",
                        additional_kwargs={
                            "ava_msg_type": "inbound",
                            "ava_inbound_id": "not-an-integer",
                        },
                    )
                ]

            monkeypatch.setattr(
                timeline_router,
                "load_checkpoint_messages_segment",
                malformed_segment,
            )

        response = test_client.get(
            f"/api/agents/{tid}/timeline",
            params={"before": f"s1.{checkpoint_id}.1.0", "limit": 50},
        )

        assert response.status_code == 200
        assert response.json() == {"items": [], "msg_count": 3, "has_more": False}

    def test_long_conversation_keeps_standing_head_notes(
        self, db_conn: psycopg.Connection, test_client: TestClient
    ) -> None:
        """Head context notes (exec timeout / timezone / cluster memory / agent
        id / agent memory — agent/graph/prompt/context_notes.py) fall off the tail
        window in a long conversation; GET must re-attach them right after the
        prompt so the head reads like a fresh window (user report 2026-08-27:
        agent 2992's head showed only "system prompt · compact summary" while a
        fresh agent shows "system prompt · 2 memories · 3 system notes").
        Not counted against `limit`; has_more recomputed."""
        from langchain_core.messages import HumanMessage, SystemMessage

        tid = create_agent(db_conn)
        messages: list[BaseMessage] = [SystemMessage(content="You are Ava.")]
        for tag in ("exec_timeout", "timezone", "memory", "agent_id", "agent_memory"):
            messages.append(
                HumanMessage(
                    content=f"[system] {tag} note",
                    additional_kwargs={
                        "ava_msg_type": "system_note",
                        "ava_note_tag": tag,
                        "ava_created_at": "2026-08-27T00:00:00+00:00",
                    },
                )
            )
        for i in range(60):
            messages.append(
                HumanMessage(
                    content=f"user msg {i}",
                    additional_kwargs={
                        "ava_msg_type": "inbound",
                        "ava_created_at": f"2026-08-27T00:01:{i:02d}+00:00",
                        "ava_source": "user",
                    },
                )
            )
        self._put_checkpoint(tid, messages, version="1")

        resp = test_client.get(f"/api/agents/{tid}/timeline")
        assert resp.status_code == 200
        data = resp.json()
        # 60 inbounds + 5 notes + 1 system prompt = 66 items; window = 50 + prompt + 5 notes
        assert [item["item_id"] for item in data["items"][:6]] == [
            "0.0",
            "1.0",
            "2.0",
            "3.0",
            "4.0",
            "5.0",
        ]
        assert [item["source"] for item in data["items"][1:6]] == [
            "exec_timeout",
            "timezone",
            "memory",
            "agent_id",
            "agent_memory",
        ]
        assert len(data["items"]) == 56
        # The newest window follows the standing context in order (16.0 is the
        # first item of the 50-item tail).
        assert data["items"][6]["item_id"] == "16.0"
        assert data["items"][-1]["item_id"] == "65.0"
        assert data["has_more"] is True

    def test_short_conversation_head_notes_not_duplicated(
        self, db_conn: psycopg.Connection, test_client: TestClient
    ) -> None:
        """Head notes inside the tail window are not re-attached a second time."""
        from langchain_core.messages import HumanMessage, SystemMessage

        tid = create_agent(db_conn)
        messages: list[BaseMessage] = [SystemMessage(content="You are Ava.")]
        for tag in ("exec_timeout", "timezone", "memory", "agent_id", "agent_memory"):
            messages.append(
                HumanMessage(
                    content=f"[system] {tag} note",
                    additional_kwargs={
                        "ava_msg_type": "system_note",
                        "ava_note_tag": tag,
                        "ava_created_at": "2026-08-27T00:00:00+00:00",
                    },
                )
            )
        for i in range(5):
            messages.append(
                HumanMessage(
                    content=f"user msg {i}",
                    additional_kwargs={
                        "ava_msg_type": "inbound",
                        "ava_created_at": f"2026-08-27T00:01:{i:02d}+00:00",
                        "ava_source": "user",
                    },
                )
            )
        self._put_checkpoint(tid, messages, version="1")

        resp = test_client.get(f"/api/agents/{tid}/timeline")
        assert resp.status_code == 200
        data = resp.json()
        assert [item["item_id"] for item in data["items"]] == [
            "0.0",
            "1.0",
            "2.0",
            "3.0",
            "4.0",
            "5.0",
            "6.0",
            "7.0",
            "8.0",
            "9.0",
            "10.0",
        ]
        assert data["has_more"] is False

    def test_cursor_past_head_notes_crosses_to_older_segment(
        self,
        db_conn: psycopg.Connection,
        test_client: TestClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A paging cursor on the first real item past the re-attached head
        notes crosses to the older segment instead of looping on the head."""
        from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

        from base.config import settings

        tid = create_agent(db_conn)
        boundary_id = self._put_checkpoint(
            tid, self._segment("history", 1), version="1", boundary=True
        )
        notes = [
            HumanMessage(
                content=f"[system] {tag} note",
                additional_kwargs={
                    "ava_msg_type": "system_note",
                    "ava_note_tag": tag,
                    "ava_created_at": "2026-08-27T00:00:00+00:00",
                },
            )
            for tag in ("exec_timeout", "timezone", "memory", "agent_id", "agent_memory")
        ]
        self._put_checkpoint(
            tid,
            [
                SystemMessage(content="system"),
                *notes,
                self._summary("current summary"),
                AIMessage(content="current item"),
            ],
            version="2",
        )
        monkeypatch.setattr(settings.gateway, "timeline_compact_history", 1)

        page = test_client.get(
            f"/api/agents/{tid}/timeline", params={"before": "7.0", "limit": 50}
        ).json()

        # The head (prompt + notes + compact summary) is returned once more for
        # frontend de-duplication, then the next older segment's tail follows.
        # The segment's SystemMessage is stripped by the segment loader, so the
        # _segment fixture renders as s1.0.0 (compact summary) + s1.1.0 (chat).
        assert [item["item_id"] for item in page["items"]] == [
            "0.0",
            "1.0",
            "2.0",
            "3.0",
            "4.0",
            "5.0",
            "6.0",
            f"s1.{boundary_id}.0.0",
            f"s1.{boundary_id}.1.0",
        ]
        assert page["has_more"] is False

    def test_historical_head_note_cursor_crosses_to_next_older_segment(
        self,
        db_conn: psycopg.Connection,
        test_client: TestClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A cursor on a historical segment's own standing head note crosses
        to the next older segment. The frontend never guards historical head
        notes (segment-prefixed ids), so the backend cross must recognize them
        as standing context or paging would loop on the segment head (review
        nit, PR #787)."""
        from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

        from base.config import settings

        tid = create_agent(db_conn)
        boundary_2 = self._put_checkpoint(
            tid, self._segment("oldest", 1), version="1", boundary=True
        )
        notes = [
            HumanMessage(
                content=f"[system] {tag} note",
                additional_kwargs={
                    "ava_msg_type": "system_note",
                    "ava_note_tag": tag,
                    "ava_created_at": "2026-08-27T00:00:00+00:00",
                },
            )
            for tag in ("exec_timeout", "timezone", "memory", "agent_id", "agent_memory")
        ]
        boundary_1 = self._put_checkpoint(
            tid,
            [
                SystemMessage(content="system"),
                *notes,
                self._summary("middle summary"),
                AIMessage(content="middle item"),
            ],
            version="2",
            boundary=True,
        )
        self._put_checkpoint(
            tid,
            [
                SystemMessage(content="system"),
                *notes,
                self._summary("current summary"),
                AIMessage(content="current item"),
            ],
            version="3",
        )
        monkeypatch.setattr(settings.gateway, "timeline_compact_history", -1)

        # Cursor on s1's third standing head note (0.0 = exec_timeout,
        # 1.0 = timezone, 2.0 = memory — the segment's SystemMessage is
        # stripped, so its notes start at 0.0): everything before it is
        # standing context, so the response crosses to s2 instead of looping
        # on the segment head.
        page = test_client.get(
            f"/api/agents/{tid}/timeline",
            params={"before": f"s1.{boundary_1}.2.0", "limit": 50},
        ).json()

        assert [item["item_id"] for item in page["items"]] == [
            f"s1.{boundary_1}.0.0",
            f"s1.{boundary_1}.1.0",
            f"s2.{boundary_2}.0.0",
            f"s2.{boundary_2}.1.0",
        ]
        assert page["has_more"] is False
