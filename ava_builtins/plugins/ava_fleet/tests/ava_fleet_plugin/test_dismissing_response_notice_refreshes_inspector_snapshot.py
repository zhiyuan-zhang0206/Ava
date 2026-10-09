"""Ava fleet plugin cases: dismissing response notice refreshes inspector snapshot."""

from __future__ import annotations

from datetime import UTC
from uuid import uuid4

import psycopg
import pytest

import ava
from ava_builtins.plugins.ava_fleet.tests.test_ava_fleet_plugin import (
    _load_activity_plugin as _load_activity_plugin,
)
from ava_builtins.plugins.ava_fleet.tests.test_ava_fleet_plugin import (
    _sdk_via_inprocess_gateway as _sdk_via_inprocess_gateway,
)
from ava_builtins.plugins.ava_fleet.tests.test_ava_fleet_plugin import _seed_agent
from base.agents.observation.snapshot import select_one
from base.lm.catalog import ModelCatalog
from tests.fixtures.pin_agent import pin_agent


def test_dismissing_response_notice_refreshes_inspector_snapshot(
    _load_activity_plugin: None,
    db_conn: psycopg.Connection,
    monkeypatch: pytest.MonkeyPatch,
    model_catalog: ModelCatalog,
):
    """Removing the dismiss snapshot refresh leaves the inspector stale."""
    from gateway.agents import notices as notices_router

    published_awaiting: list[list[str]] = []

    def _capture_snapshot(_bus: object, published_agent_id: int) -> None:
        snapshot = select_one(db_conn, published_agent_id, catalog=model_catalog)
        assert snapshot is not None
        published_awaiting.append([notice.title for notice in snapshot.notices_awaiting_response])

    monkeypatch.setattr(
        notices_router, "publish_agent_updated_sync", _capture_snapshot, raising=False
    )

    agent_id = _seed_agent(db_conn)
    pin_agent(agent_id)
    ava.ui.notify("decision needed", require_response=True, idempotency_key=str(uuid4()))  # type: ignore[attr-defined]
    ava.ui.dismiss_notice()  # type: ignore[attr-defined]

    # The first snapshot announces the newly posted question. Dismissal
    # must publish a second, now-empty snapshot for the inspector.
    assert published_awaiting == [["decision needed"], []]


def test_cross_type_supersede_refreshes_inbox_and_inspector_projections(
    _load_activity_plugin: None,
    db_conn: psycopg.Connection,
    monkeypatch: pytest.MonkeyPatch,
    model_catalog: ModelCatalog,
):
    """Each cross-type replacement announces both consumers' new state."""
    from gateway.agents import notices as notices_router

    published_awaiting: list[list[str]] = []
    posted: list[int] = []
    resolved: list[int] = []

    def _capture_snapshot(_bus: object, published_agent_id: int) -> None:
        snapshot = select_one(db_conn, published_agent_id, catalog=model_catalog)
        assert snapshot is not None
        published_awaiting.append([notice.title for notice in snapshot.notices_awaiting_response])

    async def _capture_posted(_bus: object, _agent_id: int, notice_id: int, *_args: object) -> None:
        posted.append(notice_id)

    async def _capture_resolved(_bus: object, _agent_id: int, notice_id: int) -> None:
        resolved.append(notice_id)

    monkeypatch.setattr(
        notices_router, "publish_agent_updated_sync", _capture_snapshot, raising=False
    )
    monkeypatch.setattr("ops.lifecycle.publish_notice_posted", _capture_posted)
    monkeypatch.setattr("ops.lifecycle.publish_notice_resolved", _capture_resolved)

    agent_id = _seed_agent(db_conn)
    pin_agent(agent_id)
    ava.ui.notify("FYI old", idempotency_key=str(uuid4()))  # type: ignore[attr-defined]
    ava.ui.notify("question", require_response=True, idempotency_key=str(uuid4()))  # type: ignore[attr-defined]
    ava.ui.notify("FYI new", idempotency_key=str(uuid4()))  # type: ignore[attr-defined]

    db_conn.rollback()
    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT id, title FROM agent_notices WHERE agent_id = %s ORDER BY local_id",
            (agent_id,),
        )
        notice_ids = {str(title): int(notice_id) for notice_id, title in cur.fetchall()}

    # NoticeResolved evicts each old Inbox row; NoticePosted adds each new
    # one. AgentUpdated shows the question appear, then disappear when its
    # FYI replacement supersedes it.
    assert resolved == [notice_ids["FYI old"], notice_ids["question"]]
    assert posted == [
        notice_ids["FYI old"],
        notice_ids["question"],
        notice_ids["FYI new"],
    ]
    assert published_awaiting == [["question"], []]


def test_notice_return_int_and_edit_dismiss_take_no_id(
    _load_activity_plugin: None, db_conn: psycopg.Connection
):
    """notify returns a Notice (int subclass); edit/dismiss act on the single
    open notice with no id argument."""
    agent_id = _seed_agent(db_conn)
    pin_agent(agent_id)
    notice = ava.ui.notify("hold this", content="body", priority="P2", idempotency_key=str(uuid4()))  # type: ignore[attr-defined]
    assert isinstance(notice, int)
    assert int(notice) == notice  # int conversion gives the id

    # edit acts on the open notice — no id passed.
    ava.ui.edit_notice(title="updated title")  # type: ignore[attr-defined]
    # dismiss acts on the open notice — no id passed.
    ava.ui.dismiss_notice()  # type: ignore[attr-defined]

    db_conn.rollback()
    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT title, resolved_at, resolution FROM agent_notices WHERE agent_id = %s AND local_id = %s",
            (agent_id, notice),
        )
        row = cur.fetchone()
    assert row is not None
    assert row[0] == "updated title"
    assert row[1] is not None
    assert row[2] == "withdrawn"

    # After dismissal, new notify should show pending_count = 1 (the fresh one)
    nid2 = ava.ui.notify("fresh fyi", idempotency_key=str(uuid4()))  # type: ignore[attr-defined]
    assert nid2.pending_count == 1  # pyright: ignore[reportAttributeAccessIssue, reportUnknownMemberType]
    assert nid2.pending_notices[0]["id"] == nid2  # pyright: ignore[reportAttributeAccessIssue, reportUnknownMemberType]


def test_notify_with_expire_at_valid(_load_activity_plugin: None, db_conn: psycopg.Connection):
    from datetime import datetime, timedelta

    agent_id = _seed_agent(db_conn)
    pin_agent(agent_id)
    # timedelta
    nid1 = ava.ui.notify(
        "expires in 1h", expire_at=timedelta(hours=1), idempotency_key=str(uuid4())
    )  # type: ignore[attr-defined]
    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT expire_at FROM agent_notices WHERE agent_id = %s AND local_id = %s",
            (agent_id, int(nid1)),  # pyright: ignore[reportUnknownArgumentType]
        )
        row = cur.fetchone()
    assert row is not None
    assert row[0] > datetime.now(UTC)

    # ISO string
    target = (datetime.now(UTC) + timedelta(hours=2)).isoformat()
    nid2 = ava.ui.notify("expires at ISO", expire_at=target, idempotency_key=str(uuid4()))  # type: ignore[attr-defined]
    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT expire_at FROM agent_notices WHERE agent_id = %s AND local_id = %s",
            (agent_id, int(nid2)),  # pyright: ignore[reportUnknownArgumentType]
        )
        row = cur.fetchone()
    assert row is not None


def test_notify_with_expire_at_in_past_raises_value_error(
    _load_activity_plugin: None, db_conn: psycopg.Connection
):
    from datetime import datetime, timedelta

    agent_id = _seed_agent(db_conn)
    pin_agent(agent_id)
    past = datetime.now(UTC) - timedelta(minutes=5)
    with pytest.raises(ValueError, match="expire_at is in the past"):
        ava.ui.notify("past notice", expire_at=past, idempotency_key=str(uuid4()))  # type: ignore[attr-defined]
