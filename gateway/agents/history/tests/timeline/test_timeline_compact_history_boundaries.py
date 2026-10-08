"""Timeline cases: timeline compact history boundaries."""

from __future__ import annotations

import psycopg
import pytest
from fastapi.testclient import TestClient
from langchain_core.messages import BaseMessage

from base.db import create_agent
from gateway.agents.history.tests.test_timeline import CompactHistoryCases
from gateway.agents.history.tests.test_timeline import test_client as test_client


class TestTimelineCompactHistoryBoundaries(CompactHistoryCases):
    def test_initial_short_window_reports_history_only_when_enabled(
        self,
        db_conn: psycopg.Connection,
        test_client: TestClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from langchain_core.messages import AIMessage

        from base.config import settings

        tid = create_agent(db_conn)
        self._put_checkpoint(tid, self._segment("history", 2), version="1", boundary=True)
        self._put_checkpoint(
            tid,
            self._current(AIMessage(content="current item")),
            version="2",
        )

        monkeypatch.setattr(settings.gateway, "timeline_compact_history", 0)
        disabled = test_client.get(f"/api/agents/{tid}/timeline", params={"limit": 50}).json()
        assert disabled["has_more"] is False

        monkeypatch.setattr(settings.gateway, "timeline_compact_history", 1)
        enabled = test_client.get(f"/api/agents/{tid}/timeline", params={"limit": 50}).json()
        assert enabled["has_more"] is True
        assert all(not item["item_id"].startswith("s") for item in enabled["items"])

    def test_pages_within_segment_then_reaches_its_summary(
        self,
        db_conn: psycopg.Connection,
        test_client: TestClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from langchain_core.messages import AIMessage

        from base.config import settings

        tid = create_agent(db_conn)
        boundary_id = self._put_checkpoint(
            tid, self._segment("history", 5), version="1", boundary=True
        )
        self._put_checkpoint(
            tid,
            self._current(AIMessage(content="current item")),
            version="2",
        )
        monkeypatch.setattr(settings.gateway, "timeline_compact_history", -1)

        crossed = test_client.get(
            f"/api/agents/{tid}/timeline", params={"before": "2.0", "limit": 2}
        ).json()
        assert [item["item_id"] for item in crossed["items"]] == [
            "0.0",
            "1.0",
            f"s1.{boundary_id}.4.0",
            f"s1.{boundary_id}.5.0",
        ]
        assert crossed["has_more"] is True

        middle = test_client.get(
            f"/api/agents/{tid}/timeline",
            params={"before": f"s1.{boundary_id}.4.0", "limit": 2},
        ).json()
        assert [item["item_id"] for item in middle["items"]] == [
            f"s1.{boundary_id}.2.0",
            f"s1.{boundary_id}.3.0",
        ]
        assert middle["has_more"] is True

        head = test_client.get(
            f"/api/agents/{tid}/timeline",
            params={"before": f"s1.{boundary_id}.2.0", "limit": 2},
        ).json()
        assert [item["item_id"] for item in head["items"]] == [
            f"s1.{boundary_id}.0.0",
            f"s1.{boundary_id}.1.0",
        ]
        assert head["items"][0]["kind"] == "inbound_compact_summary"
        assert head["has_more"] is False

    def test_exact_tail_boundary_delivers_summary_while_crossing_segments(
        self,
        db_conn: psycopg.Connection,
        test_client: TestClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from langchain_core.messages import AIMessage

        from base.config import settings

        tid = create_agent(db_conn)
        older_id = self._put_checkpoint(
            tid,
            self._segment("older", 1),
            version="1",
            boundary=True,
        )
        newer_id = self._put_checkpoint(
            tid,
            self._segment("newer", 50),
            version="2",
            boundary=True,
        )
        self._put_checkpoint(
            tid,
            self._current(AIMessage(content="current item")),
            version="3",
        )
        monkeypatch.setattr(settings.gateway, "timeline_compact_history", -1)

        page = test_client.get(
            f"/api/agents/{tid}/timeline",
            params={"before": f"s1.{newer_id}.1.0", "limit": 50},
        ).json()

        assert [item["item_id"] for item in page["items"]] == [
            f"s1.{newer_id}.0.0",
            f"s2.{older_id}.0.0",
            f"s2.{older_id}.1.0",
        ]
        assert page["items"][0]["kind"] == "inbound_compact_summary"
        assert page["has_more"] is False

    def test_depth_limit_still_delivers_oldest_allowed_segment_summary(
        self,
        db_conn: psycopg.Connection,
        test_client: TestClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from langchain_core.messages import AIMessage

        from base.config import settings

        tid = create_agent(db_conn)
        self._put_checkpoint(tid, self._segment("blocked", 1), version="1", boundary=True)
        allowed_id = self._put_checkpoint(
            tid,
            self._segment("allowed", 50),
            version="2",
            boundary=True,
        )
        self._put_checkpoint(
            tid,
            self._current(AIMessage(content="current item")),
            version="3",
        )
        monkeypatch.setattr(settings.gateway, "timeline_compact_history", 1)

        page = test_client.get(
            f"/api/agents/{tid}/timeline",
            params={"before": f"s1.{allowed_id}.1.0", "limit": 50},
        ).json()

        assert [item["item_id"] for item in page["items"]] == [f"s1.{allowed_id}.0.0"]
        assert page["items"][0]["kind"] == "inbound_compact_summary"
        assert page["has_more"] is False

    @pytest.mark.parametrize("depth", [-1, 1])
    def test_summary_only_segment_is_bounded_continuation_or_depth_terminal(
        self,
        db_conn: psycopg.Connection,
        test_client: TestClient,
        monkeypatch: pytest.MonkeyPatch,
        depth: int,
    ) -> None:
        from langchain_core.messages import AIMessage

        from base.config import settings

        tid = create_agent(db_conn)
        older_id = self._put_checkpoint(
            tid,
            self._segment("older", 1),
            version="1",
            boundary=True,
        )
        summary_only_id = self._put_checkpoint(
            tid,
            self._segment("summary only", 0),
            version="2",
            boundary=True,
        )
        self._put_checkpoint(
            tid,
            self._current(AIMessage(content="current item")),
            version="3",
        )
        monkeypatch.setattr(settings.gateway, "timeline_compact_history", depth)

        page = test_client.get(
            f"/api/agents/{tid}/timeline",
            params={"before": "2.0", "limit": 50},
        ).json()

        assert [item["item_id"] for item in page["items"]] == [
            "0.0",
            "1.0",
            f"s1.{summary_only_id}.0.0",
        ]
        assert page["has_more"] is (depth == -1)
        if depth == -1:
            continued = test_client.get(
                f"/api/agents/{tid}/timeline",
                params={"before": f"s1.{summary_only_id}.0.0", "limit": 50},
            ).json()
            assert [item["item_id"] for item in continued["items"]] == [
                f"s2.{older_id}.0.0",
                f"s2.{older_id}.1.0",
            ]
            assert continued["has_more"] is False

    def test_positive_depth_bounds_boundary_index_read(
        self,
        db_conn: psycopg.Connection,
        test_client: TestClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from langchain_core.messages import AIMessage

        import gateway.agents.history.timeline as timeline_router
        from base.config import settings

        tid = create_agent(db_conn)
        self._put_checkpoint(
            tid,
            self._current(AIMessage(content="current item")),
            version="1",
        )
        requested_limits: list[int | None] = []

        def boundary_ids(_db: object, _agent_id: int, *, limit: int | None = None) -> list[str]:
            requested_limits.append(limit)
            return []

        monkeypatch.setattr(timeline_router, "list_compact_boundary_checkpoint_ids", boundary_ids)
        monkeypatch.setattr(settings.gateway, "timeline_compact_history", 3)
        response = test_client.get(f"/api/agents/{tid}/timeline")

        assert response.status_code == 200
        assert requested_limits == [4]

    def test_depth_and_checkpoint_id_control_cross_segment_access(
        self,
        db_conn: psycopg.Connection,
        test_client: TestClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from langchain_core.messages import AIMessage

        from base.config import settings

        tid = create_agent(db_conn)
        older_id = self._put_checkpoint(tid, self._segment("older", 2), version="1", boundary=True)
        newer_id = self._put_checkpoint(tid, self._segment("newer", 2), version="2", boundary=True)
        current_id = self._put_checkpoint(
            tid,
            self._current(AIMessage(content="current item")),
            version="3",
        )

        monkeypatch.setattr(settings.gateway, "timeline_compact_history", 1)
        blocked = test_client.get(
            f"/api/agents/{tid}/timeline",
            params={"before": f"s2.{older_id}.1.0", "limit": 50},
        ).json()
        assert blocked == {"items": [], "msg_count": 3, "has_more": False}

        non_boundary = test_client.get(
            f"/api/agents/{tid}/timeline",
            params={"before": f"s1.{current_id}.1.0", "limit": 50},
        ).json()
        assert non_boundary == {"items": [], "msg_count": 3, "has_more": False}

        invalid_rank = test_client.get(
            f"/api/agents/{tid}/timeline",
            params={"before": f"s0.{newer_id}.1.0", "limit": 50},
        ).json()
        assert invalid_rank == {"items": [], "msg_count": 3, "has_more": False}

        monkeypatch.setattr(settings.gateway, "timeline_compact_history", -1)
        stale_rank = test_client.get(
            f"/api/agents/{tid}/timeline",
            params={"before": f"s99.{newer_id}.2.0", "limit": 1},
        ).json()
        assert [item["item_id"] for item in stale_rank["items"]] == [f"s1.{newer_id}.1.0"]

        head = test_client.get(
            f"/api/agents/{tid}/timeline",
            params={"before": f"s99.{newer_id}.2.0", "limit": 2},
        ).json()
        assert [item["item_id"] for item in head["items"]] == [
            f"s1.{newer_id}.0.0",
            f"s1.{newer_id}.1.0",
        ]
        assert head["has_more"] is True

        crossed = test_client.get(
            f"/api/agents/{tid}/timeline",
            params={"before": f"s1.{newer_id}.1.0", "limit": 50},
        ).json()
        assert [item["item_id"] for item in crossed["items"]] == [
            f"s1.{newer_id}.0.0",
            f"s2.{older_id}.0.0",
            f"s2.{older_id}.1.0",
            f"s2.{older_id}.2.0",
        ]
        assert crossed["has_more"] is False

    def test_historical_page_does_not_deserialize_the_current_segment(
        self,
        db_conn: psycopg.Connection,
        test_client: TestClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from langchain_core.messages import AIMessage

        import gateway.agents.history.timeline as timeline_router
        from base.config import settings

        tid = create_agent(db_conn)
        boundary_id = self._put_checkpoint(
            tid, self._segment("history", 2), version="1", boundary=True
        )
        self._put_checkpoint(
            tid,
            self._current(AIMessage(content="current item")),
            version="2",
        )
        monkeypatch.setattr(settings.gateway, "timeline_compact_history", 1)

        def fail_current_read(_db: object, _agent_id: int) -> list[BaseMessage]:
            raise AssertionError("historical paging must not deserialize the live segment")

        monkeypatch.setattr(timeline_router, "load_checkpoint_messages", fail_current_read)

        page = test_client.get(
            f"/api/agents/{tid}/timeline",
            params={"before": f"s1.{boundary_id}.2.0", "limit": 1},
        )

        assert page.status_code == 200
        assert page.json()["msg_count"] == 3
        assert [item["item_id"] for item in page.json()["items"]] == [f"s1.{boundary_id}.1.0"]
