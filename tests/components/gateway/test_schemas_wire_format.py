import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from base.agents.tasks.priority import Priority
from gateway.agents.schemas import (
    AgentRow,
    MessageEnqueued,
    NoticeItem,
    ResolveNoticeIn,
    UserMessageIn,
)


def test_agent_row_wire_shape() -> None:
    from base.api_contracts.agent_contract import AgentStatus, LivenessState

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
        liveness_state=LivenessState.ONLINE,
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
        expire_at=t + timedelta(days=1),
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
        "question",
        "answer",
        "task_id",
        "expire_at",
    }
    assert d["id"] == 5
    assert d["agent_label"] == "agent-3"
    assert d["priority"] == "P0"
    assert d["require_response"] is True
    assert d["blocking"] is True
    assert d["resolved_at"] is None
    assert d["resolution"] is None
    assert d["reply"] is None
    assert d["question"] is None
    assert d["answer"] is None


def test_resolve_notice_in_wire_shape() -> None:
    m = ResolveNoticeIn(action="answer", reply="yes, send it")
    assert m.action == "answer"
    assert m.reply == "yes, send it"
    m2 = ResolveNoticeIn(action="read")
    assert m2.action == "read"
    assert m2.reply is None


def test_resolve_notice_in_rejects_bad_action() -> None:
    import pytest
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        ResolveNoticeIn.model_validate({"action": "bogus"})


def test_resolve_notice_in_strips_and_rejects_empty_reply() -> None:
    import pytest
    from pydantic import ValidationError

    assert ResolveNoticeIn(action="answer", reply="  hi  ").reply == "hi"
    with pytest.raises(ValidationError):
        ResolveNoticeIn(action="answer", reply="   ")


def test_message_enqueued_wire_shape() -> None:
    m = MessageEnqueued(agent_id=7, status="enqueued")
    assert json.loads(m.model_dump_json()) == {"agent_id": 7, "status": "enqueued"}


def test_user_message_in_strips_and_rejects_empty() -> None:
    import pytest
    from pydantic import ValidationError

    m = UserMessageIn(content="  hi  ")
    assert m.content == "hi"

    with pytest.raises(ValidationError):
        UserMessageIn(content="   ")


def test_user_message_in_accepts_long_content_and_rejects_abuse_cap() -> None:
    import pytest
    from pydantic import ValidationError

    assert UserMessageIn(content="a" * 70_000).content == "a" * 70_000

    with pytest.raises(ValidationError):
        UserMessageIn(content="a" * 1_000_001)


def test_task_status_enum_is_single_sourced_across_wire_and_frontend() -> None:
    from base.agents.tasks.status import TaskStatus

    root = Path(__file__).resolve().parents[3]
    openapi = json.loads((root / "ui" / "web" / "openapi.json").read_text(encoding="utf-8"))
    enum = openapi["components"]["schemas"]["TaskStatus"]["enum"]
    assert set(enum) == {s.value for s in TaskStatus}

    types_ts = (root / "ui" / "web" / "src" / "lib" / "contracts" / "types.ts").read_text(
        encoding="utf-8"
    )
    assert 'export type TaskStatus = Schemas["TaskStatus"];' in types_ts, (
        "lib/contracts/types.ts must alias the generated TaskStatus schema (single source), "
        "not restate a status union"
    )
