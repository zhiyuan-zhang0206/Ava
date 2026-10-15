"""Admin event stream filtering and canonical wire shape."""

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from tests.components.gateway.test_cluster_endpoints import fake_admin_events as fake_admin_events
from tests.fixtures.gateway_config import gateway_test_client


def _row(
    *, msg: str = "hi", level: str = "info", event: str = "log", agent_id: int | None = None
) -> dict[str, Any]:
    return {
        "id": 1,
        "line_sha256": "a" * 64,
        "ts": datetime(2026, 8, 12, tzinfo=UTC),
        "agent_id": agent_id,
        "machine": "machine-1",
        "process": "gateway",
        "category": "telemetry",
        "event_name": event,
        "level": level,
        "source": "test",
        "target_agent_id": None,
        "attributes": {"msg": msg},
    }


class TestAdminEvents:
    """`GET /api/cluster/admin/events` slices the unified event stream
    (`telemetry_events`) for ops debugging without SSH."""

    def test_returns_newest_first(self, fake_admin_events: dict[str, list[dict[str, Any]]]) -> None:  # type: ignore[no-untyped-def]
        fake_admin_events["rows"].extend([_row(msg="oldest"), _row(msg="newest", agent_id=1)])
        with gateway_test_client() as client:
            r = client.get("/api/cluster/admin/events?limit=10")
        assert r.status_code == 200
        items = r.json()["items"]
        assert [i["payload"]["msg"] for i in items] == ["oldest", "newest"]
        # Wire shape includes the canonical-line identity shared by all event APIs.
        assert set(items[0]) == {"id", "line_sha256", "ts", "agent_id", "level", "event", "payload"}

    def test_telemetry_and_log_categories_only(
        self, fake_admin_events: dict[str, list[dict[str, Any]]]
    ) -> None:  # type: ignore[no-untyped-def]
        """Old PG contract: category IN (telemetry, log) — audit rows (spawn /
        send_message / ...) stay out of the ops log slice. The route passes the
        category set to the reader."""
        with gateway_test_client() as client:
            client.get("/api/cluster/admin/events")
        assert fake_admin_events["calls"][0]["categories"] == ["telemetry", "log"]

    def test_filter_agent_id(self, fake_admin_events: dict[str, list[dict[str, Any]]]) -> None:  # type: ignore[no-untyped-def]
        with gateway_test_client() as client:
            client.get("/api/cluster/admin/events?agent_id=3")
        kw = fake_admin_events["calls"][0]
        assert kw["agent_id"] == 3
        assert kw["service_only"] is False

    def test_filter_service_only(self, fake_admin_events: dict[str, list[dict[str, Any]]]) -> None:  # type: ignore[no-untyped-def]
        with gateway_test_client() as client:
            client.get("/api/cluster/admin/events?service_only=true")
        kw = fake_admin_events["calls"][0]
        assert kw["service_only"] is True
        assert kw["agent_id"] is None

    def test_rejects_agent_id_with_service_only(
        self, fake_admin_events: dict[str, list[dict[str, Any]]]
    ) -> None:  # type: ignore[no-untyped-def]
        with gateway_test_client() as client:
            r = client.get("/api/cluster/admin/events?agent_id=1&service_only=true")
        assert r.status_code == 400
        assert fake_admin_events["calls"] == []

    def test_filter_level_threshold(
        self, fake_admin_events: dict[str, list[dict[str, Any]]]
    ) -> None:  # type: ignore[no-untyped-def]
        # level is a MINIMUM threshold (warning -> warning|error|critical)
        with gateway_test_client() as client:
            client.get("/api/cluster/admin/events?level=WARNING")
        kw = fake_admin_events["calls"][0]
        assert kw["level_min"] == "warning"
        assert "level" not in kw

    def test_rejects_invalid_level(
        self, fake_admin_events: dict[str, list[dict[str, Any]]]
    ) -> None:  # type: ignore[no-untyped-def]
        with gateway_test_client() as client:
            r = client.get("/api/cluster/admin/events?level=NOPE")
        assert r.status_code == 400
        assert fake_admin_events["calls"] == []

    def test_filter_since_relative(
        self, fake_admin_events: dict[str, list[dict[str, Any]]]
    ) -> None:  # type: ignore[no-untyped-def]
        before = datetime.now(UTC)
        with gateway_test_client() as client:
            client.get("/api/cluster/admin/events?since=1m")
        after = datetime.now(UTC)
        from_ = fake_admin_events["calls"][0]["from_"]
        assert from_ is not None
        # cutoff = now - 60s, on the Python clock
        assert before - timedelta(seconds=70) <= from_ <= after - timedelta(seconds=50)

    def test_filter_since_absolute(
        self, fake_admin_events: dict[str, list[dict[str, Any]]]
    ) -> None:  # type: ignore[no-untyped-def]
        with gateway_test_client() as client:
            client.get("/api/cluster/admin/events?since=2026-08-01T00:00:00Z")
        assert fake_admin_events["calls"][0]["from_"] == datetime(2026, 8, 1, tzinfo=UTC)

    def test_rejects_invalid_since(
        self, fake_admin_events: dict[str, list[dict[str, Any]]]
    ) -> None:  # type: ignore[no-untyped-def]
        with gateway_test_client() as client:
            r = client.get("/api/cluster/admin/events?since=garbage")
        assert r.status_code == 400
        assert fake_admin_events["calls"] == []

    def test_filter_event_multi(self, fake_admin_events: dict[str, list[dict[str, Any]]]) -> None:  # type: ignore[no-untyped-def]
        with gateway_test_client() as client:
            client.get("/api/cluster/admin/events?event=spawn,terminate")
        assert fake_admin_events["calls"][0]["event_names"] == ["spawn", "terminate"]

    def test_filter_grep(self, fake_admin_events: dict[str, list[dict[str, Any]]]) -> None:  # type: ignore[no-untyped-def]
        with gateway_test_client() as client:
            client.get("/api/cluster/admin/events?grep=hello")
        assert fake_admin_events["calls"][0]["grep"] == "hello"

    def test_limit_defaults_to_200(
        self, fake_admin_events: dict[str, list[dict[str, Any]]]
    ) -> None:  # type: ignore[no-untyped-def]
        with gateway_test_client() as client:
            client.get("/api/cluster/admin/events")
        assert fake_admin_events["calls"][0]["limit"] == 200

    def test_limit_defaults_come_from_display_config(
        self, fake_admin_events: dict[str, list[dict[str, Any]]], monkeypatch: pytest.MonkeyPatch
    ) -> None:  # type: ignore[no-untyped-def]
        """The implicit page is ``settings.display.cluster_events_default_limit``
        (``AVA_CLUSTER_EVENTS_DEFAULT_LIMIT``); the literal 200 is only that
        field's default, not a hard-coded page size."""
        from base.config import settings

        monkeypatch.setattr(settings.display, "cluster_events_default_limit", 7)
        with gateway_test_client() as client:
            client.get("/api/cluster/admin/events")
        assert fake_admin_events["calls"][0]["limit"] == 7

    def test_limit_caps_at_1000(self, fake_admin_events: dict[str, list[dict[str, Any]]]) -> None:  # type: ignore[no-untyped-def]
        with gateway_test_client() as client:
            r = client.get("/api/cluster/admin/events?limit=1001")
        assert r.status_code == 400
        assert fake_admin_events["calls"] == []

    def test_no_since_uses_default_window(
        self, fake_admin_events: dict[str, list[dict[str, Any]]]
    ) -> None:  # type: ignore[no-untyped-def]
        # no since -> the route's 24h lower bound applies
        before = datetime.now(UTC)
        with gateway_test_client() as client:
            client.get("/api/cluster/admin/events")
        from_ = fake_admin_events["calls"][0]["from_"]
        assert from_ is not None
        assert before - timedelta(hours=24, seconds=5) <= from_ <= before - timedelta(hours=23)
