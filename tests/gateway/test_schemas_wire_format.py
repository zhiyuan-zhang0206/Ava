"""Freeze the HTTP wire format of `gateway/schemas.py`.

Analogous to `test_events_wire_format.py`: the Python schema and the hand-written TS
mirror (`ui/web/src/lib/types.ts`) must stay in sync on fields; a rename like
`content` → `text` won't be caught by type tests — but external consumers / the
frontend will blow up at runtime.

This file pins the JSON shape of every schema; renaming a field or changing a type
goes red immediately.
"""

import json
from datetime import UTC, datetime

from gateway.schemas import (
    AgentRow,
    CompactEnqueued,
    MessageEnqueued,
    NewThread,
    NoticeItem,
    ResolveNoticeIn,
    UserMessageIn,
)
from shared.priority import Priority


def test_agent_row_wire_shape() -> None:
    from shared.agents import AgentStatus

    t = datetime(2025, 1, 1, 0, 0, 0, tzinfo=UTC)
    m = AgentRow(
        agent_id=1,
        spawner="user",
        fork_source_agent_id=None,
        fork_source_checkpoint_id=None,
        status=AgentStatus.IDLING,
        pid=12345,
        spawned_at=t,
        started_at=t,
        last_active_at=t,
        last_inbound_at=t,
        label="agent-1",
        machine="test-machine",
        supports_vision=True,
        liveness_state="online",
        last_probe_at=None,
        notices_awaiting_response=[],
        unread_notice_count=0,
        heartbeat_paused_until=None,
    )
    d = json.loads(m.model_dump_json())
    assert d["agent_id"] == 1
    assert d["label"] == "agent-1"
    assert d["spawner"] == "user"
    assert d["supports_vision"] is True
    assert d["notices_awaiting_response"] == []
    assert d["unread_notice_count"] == 0


def test_notice_item_wire_shape() -> None:
    """NoticeItem feeds GET /api/notices/open + /resolved + carries the unified
    agent_notices row to the frontend. Anchor the field names + the
    open-vs-resolved nullable shape."""
    t = datetime(2025, 1, 1, 0, 0, 0, tzinfo=UTC)
    m = NoticeItem(
        id=5,
        agent_id=3,
        agent_label="agent-3",
        title="deploy?",
        content="A) yes\nB) no",
        priority=Priority.P0,
        require_response=True,
        blocking=True,
        created_at=t,
        updated_at=None,
        resolved_at=None,
        resolution=None,
        reply=None,
    )
    d = json.loads(m.model_dump_json())
    assert set(d.keys()) == {
        "id",
        "agent_id",
        "agent_label",
        "title",
        "content",
        "priority",
        "require_response",
        "blocking",
        "created_at",
        "updated_at",
        "resolved_at",
        "resolution",
        "reply",
        "task_id",
    }
    assert d["id"] == 5
    assert d["agent_label"] == "agent-3"
    assert d["priority"] == "P0"
    assert d["require_response"] is True
    assert d["blocking"] is True
    # open notice: resolution machinery all NULL
    assert d["resolved_at"] is None
    assert d["resolution"] is None
    assert d["reply"] is None


def test_resolve_notice_in_wire_shape() -> None:
    """ResolveNoticeIn is the POST .../resolve body — action enum + optional reply."""
    m = ResolveNoticeIn(action="answer", reply="yes, send it")
    assert m.action == "answer"
    assert m.reply == "yes, send it"
    # reply optional (dismiss/read may carry none)
    m2 = ResolveNoticeIn(action="read")
    assert m2.action == "read"
    assert m2.reply is None


def test_resolve_notice_in_rejects_bad_action() -> None:
    import pytest
    from pydantic import ValidationError

    # validate via dict so the bad value is rejected at runtime (Pydantic),
    # not flagged statically against the action Literal.
    with pytest.raises(ValidationError):
        ResolveNoticeIn.model_validate({"action": "bogus"})


def test_resolve_notice_in_strips_and_rejects_empty_reply() -> None:
    import pytest
    from pydantic import ValidationError

    # reply rides _UserContent: stripped, and whitespace-only is rejected.
    assert ResolveNoticeIn(action="answer", reply="  hi  ").reply == "hi"
    with pytest.raises(ValidationError):
        ResolveNoticeIn(action="answer", reply="   ")


def test_new_thread_wire_shape() -> None:
    assert json.loads(NewThread(id=42).model_dump_json()) == {"id": 42}


def test_message_enqueued_wire_shape() -> None:
    m = MessageEnqueued(agent_id=7, status="enqueued")
    assert json.loads(m.model_dump_json()) == {"agent_id": 7, "status": "enqueued"}


def test_compact_enqueued_wire_shape() -> None:
    m = CompactEnqueued(mode="framework", agent_id=3, status="enqueued")
    assert json.loads(m.model_dump_json()) == {
        "mode": "framework",
        "agent_id": 3,
        "status": "enqueued",
    }


def test_user_message_in_strips_and_rejects_empty() -> None:
    """UserMessageIn StringConstraints: strip_whitespace + min_length=1."""
    import pytest
    from pydantic import ValidationError

    # non-empty after strip — valid
    m = UserMessageIn(content="  hi  ")
    assert m.content == "hi"

    # empty after strip — 422
    with pytest.raises(ValidationError):
        UserMessageIn(content="   ")


def test_user_message_in_accepts_long_content_and_rejects_abuse_cap() -> None:
    """TEXT-backed messages allow legitimate long input up to one million chars."""
    import pytest
    from pydantic import ValidationError

    assert UserMessageIn(content="a" * 70_000).content == "a" * 70_000

    with pytest.raises(ValidationError):
        UserMessageIn(content="a" * 1_000_001)
