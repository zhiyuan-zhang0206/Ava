"""POST /api/frontend-telemetry — frontend user-modeling telemetry ingestion.

Locks the ingest contract: a valid batch lands one `frontend_interaction`
event (category=telemetry, source=user) per accepted interaction in the
unified stream; malformed batches fail fast (422), oversized bodies 413, and
the per-session rate limit backstop drops excess events (204 + warning log)
instead of letting a misbehaving tab blow up the table.

Runs on ava_test: POST a batch through TestClient, then assert the JSONL
mirror lines (telemetry.sync() before reading — the emitter's drain thread
writes asynchronously). The PG `events` copy is a read-only archive since the
LGTM cutover (task #1197 close-C).
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from gateway.app import app
from gateway.routers import frontend_telemetry as ft_router
from shared import telemetry

# One valid interaction as the browser sends it.
PAGE = "fleet"
ELEMENT = "spawn"
SESSION = "123e4567-e89b-12d3-a456-426614174000"


def _payload(events: list[dict], session: str = SESSION) -> dict:
    return {"session_id": session, "events": events}


def _one(page: str = PAGE, element: str = ELEMENT, **extra: str) -> dict:
    return {"page": page, "element": element, **extra}


def _rows(session: str) -> list[tuple]:  # type: ignore[no-untyped-def]
    """This session's `frontend_interaction` mirror lines, oldest first,
    after a sync barrier. Each test uses a fresh session id (the mirror is
    cumulative within a worker — the PG TRUNCATE barrier is gone with the
    events write), so the filter is exact.
    """
    import json
    from datetime import UTC, datetime

    from shared.paths import logs_dir

    telemetry.sync()
    day = datetime.now(UTC).strftime("%Y%m%d")
    path = logs_dir() / f"events-{day}.jsonl"
    if not path.exists():
        return []
    out: list[tuple[Any, ...]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if obj.get("event_name") != "frontend_interaction":
            continue
        if obj.get("attributes", {}).get("session_id") != session:
            continue
        out.append(
            (
                obj["event_name"],
                obj["category"],
                obj["level"],
                obj["source"],
                obj["attributes"],
            )
        )
    return out


def _session() -> str:
    import uuid

    return str(uuid.uuid4())


class TestFrontendTelemetryIngest:
    def test_valid_batch_lands_events(self) -> None:  # type: ignore[no-untyped-def]
        session = _session()
        with TestClient(app) as client:
            r = client.post(
                "/api/frontend-telemetry",
                json=_payload(
                    [
                        _one(),
                        _one(element="page-view", page="control/config"),
                        _one(
                            element="setting-change",
                            page="control/display",
                            key="display.show_machine_name",
                            value="false",
                        ),
                    ],
                    session=session,
                ),
            )
        assert r.status_code == 204
        rows = _rows(session)
        assert len(rows) == 3  # pyright: ignore[reportUnknownArgumentType]
        for row in rows:
            assert row[0] == "frontend_interaction"
            assert row[1] == "telemetry"
            assert row[2] == "info"
            assert row[3] == "user"
        attrs = [row[4] for row in rows]  # psycopg decodes jsonb to dict
        assert attrs[0] == {"page": "fleet", "element": "spawn", "session_id": session}
        assert attrs[1] == {
            "page": "control/config",
            "element": "page-view",
            "session_id": session,
        }
        assert attrs[2] == {
            "page": "control/display",
            "element": "setting-change",
            "session_id": session,
            "key": "display.show_machine_name",
            "value": "false",
        }

    def test_missing_key_value_omitted_not_null(self) -> None:  # type: ignore[no-untyped-def]
        """Non-setting events must not carry key/value at all."""
        session = _session()
        with TestClient(app) as client:
            r = client.post(
                "/api/frontend-telemetry",
                json=_payload([_one(element="composer-send")], session=session),
            )
        assert r.status_code == 204
        attrs = _rows(session)[0][4]
        assert "key" not in attrs
        assert "value" not in attrs

    def test_malformed_batch_422(self) -> None:  # type: ignore[no-untyped-def]
        session = _session()
        cases = [
            # empty events list
            _payload([]),
            # bad element charset (free text / spaces)
            _payload([_one(element="click me!")]),
            # bad page charset (path with query)
            _payload([_one(page="/fleet?x=1")]),
            # bad session id
            _payload([_one()], session="not-a-uuid!"),
            # key too long
            _payload([_one(element="setting-change", key="k" * 200)]),
            # value too long (free text smuggled as a value)
            _payload([_one(element="setting-change", value="v" * 200)]),
        ]
        with TestClient(app) as client:
            for body in cases:
                r = client.post("/api/frontend-telemetry", json=body)
                assert r.status_code == 422, body
                assert r.json()["code"] == "invalid_telemetry_batch"
        # nothing landed
        assert _rows(session) == []

    def test_oversized_body_413(self) -> None:  # type: ignore[no-untyped-def]
        session = _session()
        with TestClient(app) as client:
            r = client.post(
                "/api/frontend-telemetry",
                content=b"x" * (ft_router._MAX_BODY_BYTES + 1),
                headers={"content-type": "application/json"},
            )
        assert r.status_code == 413
        assert r.json()["code"] == "telemetry_batch_too_large"
        assert _rows(session) == []

    def test_rate_limit_backstop_drops_excess(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:  # type: ignore[no-untyped-def]
        """Over the per-session budget: accepted up to the cap, the rest
        dropped — the events table cannot be flooded by one tab."""
        ft_router._session_windows.clear()
        session = _session()
        events = [_one(element=f"e{i}") for i in range(ft_router._MAX_EVENTS_PER_MINUTE + 10)]
        with TestClient(app) as client:
            r = client.post("/api/frontend-telemetry", json=_payload(events, session=session))
        assert r.status_code == 204
        rows = _rows(session)
        assert len(rows) == ft_router._MAX_EVENTS_PER_MINUTE  # pyright: ignore[reportUnknownArgumentType]

    def test_second_session_gets_own_budget(self) -> None:  # type: ignore[no-untyped-def]
        ft_router._session_windows.clear()
        session = _session()
        other = _session()
        with TestClient(app) as client:
            r1 = client.post(
                "/api/frontend-telemetry",
                json=_payload([_one()] * (ft_router._MAX_EVENTS_PER_MINUTE + 5), session=session),
            )
            r2 = client.post(
                "/api/frontend-telemetry",
                json=_payload([_one()] * 3, session=other),
            )
        assert r1.status_code == 204
        assert r2.status_code == 204
        assert len(_rows(session)) == ft_router._MAX_EVENTS_PER_MINUTE  # pyright: ignore[reportUnknownArgumentType]
        assert len(_rows(other)) == 3  # pyright: ignore[reportUnknownArgumentType]
