"""The fleet notice SDK (notify / edit_notice / dismiss_notice) against the in-process gateway.

Split out of `meiva_builtins/plugins/meiva_fleet/tests/test_meiva_fleet_plugin.py`: these cases route the SDK's gateway client at `gateway.app`, so they live with the gateway tests."""

from collections.abc import Iterator
from typing import Any
from uuid import uuid4

import psycopg
import pytest

import meiva
from base.agents.observation.snapshot import select_one
from base.cluster.machine import MachineIdentity
from base.config import settings
from base.packages.plugin_providers import build_model_catalog
from meiva.tests.fixtures.pin_agent import pin_agent
from meiva_builtins.plugins.meiva_fleet.tests.registry_support import (
    installed_fleet_surface,
)


def seed_agent(db: psycopg.Connection) -> int:
    """Insert an agents + agents_meta row (log() / set_label() write against an
    existing meta row; the real spawn path always creates it first)."""
    with db.cursor() as cur:
        cur.execute("INSERT INTO agents DEFAULT VALUES RETURNING id")
        row = cur.fetchone()
        assert row is not None
        agent_id: int = row[0]
        cur.execute(
            "INSERT INTO agents_meta (id, spawner, status) VALUES (%s, 'test', 'running')",
            (agent_id,),
        )
    db.commit()
    return agent_id


@pytest.fixture
def sdk_via_inprocess_gateway(
    gateway_data_plane: None, monkeypatch: pytest.MonkeyPatch, machine_identity: MachineIdentity
):
    """The notice SDK now goes through the unified gateway write API (R3 door
    ④): route the SDK's gateway client at the in-process app so notify /
    edit_notice / dismiss_notice hit the real endpoints against the test DB.

    The assembled Gateway reads both data stores, so the fixture declares
    `gateway_data_plane`."""
    from meiva.gateway_client import use_client
    from tests.fixtures.gateway_config import gateway_test_client

    with (
        gateway_test_client(base_url="http://test-gateway", machine=machine_identity) as tc,
        use_client(tc),
    ):
        yield


@pytest.fixture
def load_activity_plugin(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Install Fleet with a valid local sampling policy; uninstall after the test."""
    from base.agents.sdk import call_policy

    def _policy_for_test(_owner: call_policy.SamplingPolicyOwner) -> call_policy.SamplingPolicy:
        return call_policy.SamplingPolicy()

    monkeypatch.setattr(call_policy, "policy", _policy_for_test)
    with installed_fleet_surface():
        yield


def _notices(db_conn: psycopg.Connection, agent_id: int) -> list[tuple]:
    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT title, content, priority, require_response, blocking, "
            "resolved_at, resolution, reply FROM agent_notices "
            "WHERE agent_id = %s ORDER BY local_id",
            (agent_id,),
        )
        return cur.fetchall()


def _assert_first_notice_is_sole_pending_fyi(nid: Any) -> None:
    assert nid.pending_count == 1
    assert len(nid.pending_notices) == 1
    assert nid.pending_notices[0]["id"] == nid
    assert nid.pending_notices[0]["title"] == "migration done"
    assert nid.pending_notices[0]["priority"] == "P1"
    assert "created_at" in nid.pending_notices[0]
    assert nid.superseded == []


def _assert_second_notice_supersedes_first(nid: Any, nid2: Any) -> None:
    assert nid2.pending_count == 1
    assert [n["title"] for n in nid2.pending_notices] == ["hit a rate limit"]
    assert nid2.superseded == [nid]


def test_notify_inserts_fyi_and_snapshot_counts_unread(
    sdk_via_inprocess_gateway: None, load_activity_plugin: None, db_conn: psycopg.Connection
):
    agent_id = seed_agent(db_conn)
    pin_agent(agent_id)
    # require_response defaults False -> these are FYI notices.
    nid = meiva.ui.notify(
        "migration done", content="14k rows", priority="P1", idempotency_key=str(uuid4())
    )  # type: ignore[attr-defined]
    assert isinstance(nid, int)  # Notice is an int subclass — backward compatible
    _assert_first_notice_is_sole_pending_fyi(nid)

    # Posting a second notice auto-resolves the first (at most one).
    nid2 = meiva.ui.notify("hit a rate limit", idempotency_key=str(uuid4()))  # type: ignore[attr-defined]
    _assert_second_notice_supersedes_first(nid, nid2)

    db_conn.rollback()  # notify() committed via its own cursor; refresh our view
    rows = _notices(db_conn, agent_id)
    # First notice is now superseded; second is open.
    assert rows[0][0] == "migration done"
    assert rows[0][5] is not None  # resolved_at set
    assert rows[0][6] == "superseded"
    assert rows[1][0] == "hit a rate limit"
    assert rows[1][5] is None  # resolved_at (open)

    # the snapshot badge counts only the open FYI notice as unread.
    snap = select_one(
        db_conn,
        agent_id,
        catalog=build_model_catalog(),
        default_model_reader=lambda: settings.lm.llm_model,
    )
    assert snap is not None
    assert snap.unread_notice_count == 1
    assert snap.notices_awaiting_response == []


def test_notify_require_response_rides_awaiting_worklist(
    sdk_via_inprocess_gateway: None, load_activity_plugin: None, db_conn: psycopg.Connection
):
    agent_id = seed_agent(db_conn)
    pin_agent(agent_id)
    meiva.ui.notify(  # type: ignore[attr-defined]
        "Send the release email?",
        content="A) yes\nB) no",
        priority="P0",
        require_response=True,
        blocking=True,
        idempotency_key=str(uuid4()),
    )
    # Posting a second require_response notice auto-resolves the first.
    nid2 = meiva.ui.notify("Name the branch?", require_response=True, idempotency_key=str(uuid4()))  # type: ignore[attr-defined]
    assert nid2.superseded != []  # pyright: ignore[reportAttributeAccessIssue, reportUnknownMemberType]

    db_conn.rollback()
    # Only the most recent require_response notice rides the snapshot worklist.
    snap = select_one(
        db_conn,
        agent_id,
        catalog=build_model_catalog(),
        default_model_reader=lambda: settings.lm.llm_model,
    )
    assert snap is not None
    assert snap.unread_notice_count == 0
    awaiting = snap.notices_awaiting_response
    assert [n.title for n in awaiting] == ["Name the branch?"]
    assert awaiting[0].content is None
    assert awaiting[0].blocking is False  # blocking defaults False


def test_sdk_question_keeps_number_order_in_snapshot(
    sdk_via_inprocess_gateway: None, load_activity_plugin: None, db_conn: psycopg.Connection
) -> None:
    agent_id = seed_agent(db_conn)
    pin_agent(agent_id)
    meiva.ui.notify(  # type: ignore[attr-defined]
        "Choose the scope?",
        require_response=True,
        question={
            "type": "single_select",
            "options": [{"id": "z", "label": "First"}, {"id": "a", "label": "Second"}],
            "allow_other": True,
        },
        idempotency_key=str(uuid4()),
    )
    db_conn.rollback()
    snapshot = select_one(
        db_conn,
        agent_id,
        catalog=build_model_catalog(),
        default_model_reader=lambda: settings.lm.llm_model,
    )
    assert snapshot is not None
    question = snapshot.notices_awaiting_response[0].question
    assert question is not None
    assert question.model_dump(mode="json")["options"] == [
        {"id": "z", "label": "First", "description": None},
        {"id": "a", "label": "Second", "description": None},
    ]


def _seed_task(db: psycopg.Connection, owner: int) -> int:
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO agent_tasks (title, description, created_by, owner) "
            "VALUES ('t', 'd', 'user', %s) RETURNING id",
            (owner,),
        )
        row = cur.fetchone()
    assert row is not None
    db.commit()
    return row[0]


def _notice_task_id(db: psycopg.Connection, agent_id: int) -> int | None:
    with db.cursor() as cur:
        cur.execute(
            "SELECT task_id FROM agent_notices WHERE agent_id = %s AND resolved_at IS NULL",
            (agent_id,),
        )
        row = cur.fetchone()
    assert row is not None
    return row[0]


def test_notify_records_task_id_and_rides_snapshot(
    sdk_via_inprocess_gateway: None, load_activity_plugin: None, db_conn: psycopg.Connection
):
    """notify(task=...) writes the task link, and a require_response notice carries
    it out on the snapshot's notices_awaiting_response."""
    agent_id = seed_agent(db_conn)
    tid = _seed_task(db_conn, agent_id)
    pin_agent(agent_id)
    meiva.ui.notify(
        "stalled on a decision", require_response=True, task=tid, idempotency_key=str(uuid4())
    )  # type: ignore[attr-defined]
    db_conn.rollback()  # notify committed via its own cursor; refresh our view
    assert _notice_task_id(db_conn, agent_id) == tid
    snap = select_one(
        db_conn,
        agent_id,
        catalog=build_model_catalog(),
        default_model_reader=lambda: settings.lm.llm_model,
    )
    assert snap is not None
    assert [n.task_id for n in snap.notices_awaiting_response] == [tid]


def test_notify_without_task_leaves_task_id_null(
    sdk_via_inprocess_gateway: None, load_activity_plugin: None, db_conn: psycopg.Connection
):
    agent_id = seed_agent(db_conn)
    pin_agent(agent_id)
    meiva.ui.notify("fyi, no task", idempotency_key=str(uuid4()))  # type: ignore[attr-defined]
    db_conn.rollback()
    assert _notice_task_id(db_conn, agent_id) is None


def test_notify_nonexistent_task_raises(
    sdk_via_inprocess_gateway: None, load_activity_plugin: None, db_conn: psycopg.Connection
):
    agent_id = seed_agent(db_conn)
    pin_agent(agent_id)
    with pytest.raises(ValueError, match="task 999999 does not exist"):
        meiva.ui.notify("names a ghost task", task=999999, idempotency_key=str(uuid4()))  # type: ignore[attr-defined]


def test_edit_notice_partial_update(
    sdk_via_inprocess_gateway: None, load_activity_plugin: None, db_conn: psycopg.Connection
):
    agent_id = seed_agent(db_conn)
    pin_agent(agent_id)
    nid = meiva.ui.notify(
        "draft title", content="old body", priority="P2", idempotency_key=str(uuid4())
    )  # type: ignore[attr-defined]
    # change only title + priority; content is left as-is (omitted != cleared).
    meiva.ui.edit_notice(title="new title", priority="P0")  # type: ignore[attr-defined]

    db_conn.rollback()
    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT title, content, priority, updated_at FROM agent_notices WHERE agent_id = %s AND local_id = %s",
            (agent_id, nid),  # pyright: ignore[reportUnknownArgumentType]
        )
        row = cur.fetchone()
    assert row is not None
    assert row[0] == "new title"
    assert row[1] == "old body"  # untouched
    assert row[2] == "P0"
    assert row[3] is not None  # updated_at stamped

    # passing content=None explicitly clears it (the _UNSET sentinel
    # distinguishes "leave alone" from "erase").
    meiva.ui.edit_notice(content=None)  # type: ignore[attr-defined]
    db_conn.rollback()
    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT content FROM agent_notices WHERE agent_id = %s AND local_id = %s",
            (agent_id, nid),  # pyright: ignore[reportUnknownArgumentType]
        )
        row = cur.fetchone()
    assert row is not None and row[0] is None


def test_response_notice_content_edits_publish_refreshed_snapshot(
    sdk_via_inprocess_gateway: None,
    load_activity_plugin: None,
    db_conn: psycopg.Connection,
    monkeypatch: pytest.MonkeyPatch,
):
    """Content-only edits invalidate the selected agent's authoritative read.

    NoticePosted refreshes the Inbox queue; AgentUpdated requests current
    detail so the inspector can read the edited response-required notice.
    """
    from gateway.agents import notices as notices_router

    published_agent_ids: list[int] = []

    def _capture_snapshot(_bus: object, published_agent_id: int) -> None:
        published_agent_ids.append(published_agent_id)

    # The route must publish this after every durable create/edit. It is absent
    # before the regression fix, so the assertion below proves the missing
    # inspector projection rather than merely the row's database state.
    monkeypatch.setattr(
        notices_router, "publish_agent_updated_sync", _capture_snapshot, raising=False
    )

    agent_id = seed_agent(db_conn)
    pin_agent(agent_id)
    meiva.ui.notify(  # type: ignore[attr-defined]
        "decision needed",
        content="revision 0",
        priority="P1",
        require_response=True,
        blocking=True,
        idempotency_key=str(uuid4()),
    )
    for revision in range(1, 11):
        content = f"revision {revision}"
        meiva.ui.edit_notice(content=content)  # type: ignore[attr-defined]

    # One AgentUpdated for creation plus one per content-only edit keeps the
    # inspector's cached snapshot authoritative through the whole chain.
    assert published_agent_ids == [agent_id] * 11
    db_conn.rollback()
    snapshot = select_one(
        db_conn,
        agent_id,
        catalog=build_model_catalog(),
        default_model_reader=lambda: settings.lm.llm_model,
    )
    assert snapshot is not None
    awaiting = snapshot.notices_awaiting_response
    assert len(awaiting) == 1
    notice = awaiting[0]
    assert notice.title == "decision needed"
    assert notice.content == "revision 10"
    assert notice.priority == "P1"
    assert notice.blocking is True


def test_edit_notice_validation_and_guards(
    sdk_via_inprocess_gateway: None, load_activity_plugin: None, db_conn: psycopg.Connection
):
    agent_id = seed_agent(db_conn)
    pin_agent(agent_id)
    meiva.ui.notify("fyi notice", idempotency_key=str(uuid4()))  # type: ignore[attr-defined]

    # nothing passed -> nothing to change.
    with pytest.raises(ValueError, match="at least one field"):
        meiva.ui.edit_notice()  # type: ignore[attr-defined]
    # bad priority.
    with pytest.raises(ValueError, match="priority"):
        meiva.ui.edit_notice(priority="P9")  # type: ignore[attr-defined]
    # blocking=True on an FYI (require_response False) is rejected.
    with pytest.raises(ValueError, match="needs a response"):
        meiva.ui.edit_notice(blocking=True)  # type: ignore[attr-defined]
    # no open notice is idempotent (no-op).
    meiva.ui.dismiss_notice()  # type: ignore[attr-defined]
    meiva.ui.edit_notice(title="too late")  # type: ignore[attr-defined]


def test_dismiss_notice_withdraws(
    sdk_via_inprocess_gateway: None, load_activity_plugin: None, db_conn: psycopg.Connection
):
    agent_id = seed_agent(db_conn)
    pin_agent(agent_id)
    nid = meiva.ui.notify("stale fyi", idempotency_key=str(uuid4()))  # type: ignore[attr-defined]
    meiva.ui.dismiss_notice()  # type: ignore[attr-defined]

    db_conn.rollback()
    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT resolved_at, resolution FROM agent_notices WHERE agent_id = %s AND local_id = %s",
            (agent_id, nid),  # pyright: ignore[reportUnknownArgumentType]
        )
        row = cur.fetchone()
    assert row is not None
    assert row[0] is not None  # resolved_at set
    assert row[1] == "withdrawn"

    # the dismissed notice drops off the unread badge.
    snap = select_one(
        db_conn,
        agent_id,
        catalog=build_model_catalog(),
        default_model_reader=lambda: settings.lm.llm_model,
    )
    assert snap is not None
    assert snap.unread_notice_count == 0

    # dismissing again is idempotent (no-op).
    meiva.ui.dismiss_notice()  # type: ignore[attr-defined]


def test_supersede_and_withdraw_publish_notice_resolved_for_both_kinds(
    sdk_via_inprocess_gateway: None,
    load_activity_plugin: None,
    db_conn: psycopg.Connection,
    monkeypatch: pytest.MonkeyPatch,
):
    """Every agent-side resolution — supersede (a newer notice) or withdraw
    (dismiss_notice) — publishes notice_resolved regardless of require_response.
    The unified inbox refreshes its resolved history off this event alone, so a
    require_response notice resolved without one would leave the open list yet
    never surface in the resolved history."""
    # Events now publish from the gateway (R3 door ④ unified write API), not
    # the SDK — patch the gateway-side publisher.
    import ops.lifecycle as ops_mod

    resolved: list[int] = []

    async def _fake_publish(_bus: object, _aid: int, global_id: int) -> None:
        resolved.append(global_id)

    monkeypatch.setattr(ops_mod, "publish_notice_resolved", _fake_publish)

    agent_id = seed_agent(db_conn)
    pin_agent(agent_id)
    meiva.ui.notify("Q1?", require_response=True, idempotency_key=str(uuid4()))  # type: ignore[attr-defined]
    assert resolved == []  # the first post resolves nothing
    # A second require_response notice supersedes the first — must publish even
    # though the superseded notice needed a response.
    meiva.ui.notify("Q2?", require_response=True, idempotency_key=str(uuid4()))  # type: ignore[attr-defined]
    assert len(resolved) == 1
    # Withdrawing the surviving require_response notice publishes too.
    meiva.ui.dismiss_notice()  # type: ignore[attr-defined]
    assert len(resolved) == 2
