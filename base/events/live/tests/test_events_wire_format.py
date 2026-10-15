import json
from typing import Any

import pytest

from base.events.live.projection import (
    GLOBAL_ROLES,
    SYSTEM_ROLES,
    AgentSpawned,
    AgentUpdated,
    Cancelled,
    ChatDelta,
    ChatStart,
    CodeDelta,
    CodeStart,
    CompactDone,
    CompactFinished,
    CompactRequest,
    CompactStarted,
    Error,
    ExecOutput,
    ExecOutputChunk,
    ExecStart,
    InboundArrived,
    InboundCommitted,
    LabelUpdated,
    LLMDone,
    NoticePosted,
    NoticeResolved,
    ReasoningDelta,
    ReasoningStart,
    TaskCreated,
    TaskUpdated,
    TokenUsage,
    ToUserChanged,
)

FROZEN_WIRE: list[tuple[type, str, dict[str, Any]]] = [
    (ToUserChanged, "to_user_changed", {}),
    (AgentSpawned, "agent_spawned", {}),
    (AgentUpdated, "agent_updated", {}),
    (ChatStart, "chat_start", {"item_id": "5.0"}),
    (ChatDelta, "chat_delta", {"item_id": "5.0", "content": "hello"}),
    (CompactRequest, "compact_request", {"content": "[compact requested, 5 chars]"}),
    (CompactDone, "compact_done", {}),
    (
        CompactStarted,
        "compact_started",
        {
            "compact_id": "1a2b3c4d5e6f7a8b9c0d1e2f3a4b5c6d",
            "started_at": "2026-09-14T02:15:00+00:00",
            "mode": "request",
        },
    ),
    (
        CompactFinished,
        "compact_finished",
        {
            "compact_id": "1a2b3c4d5e6f7a8b9c0d1e2f3a4b5c6d",
            "status": "success",
            "finished_at": "2026-09-14T02:15:24+00:00",
        },
    ),
    (CodeStart, "code_start", {"item_id": "5.1"}),
    (CodeDelta, "code_delta", {"item_id": "5.1", "content": "ava."}),
    (ReasoningStart, "reasoning_start", {"item_id": "5.0"}),
    (ReasoningDelta, "reasoning_delta", {"item_id": "5.0", "content": "thinking"}),
    (ExecStart, "exec_start", {"item_id": "5.0"}),
    (ExecOutput, "exec_output", {"item_id": "6.0", "content": "stdout:\nok"}),
    (
        Error,
        "error",
        {
            "content": "agent processing error: Foo",
            "error_class": "permanent",
            "provider": "deepseek",
            "status": 400,
            "reason": "bad_request",
            "blocked": True,
            "recovery": "Choose a different model overlay, then send a new message.",
        },
    ),
    (Cancelled, "cancelled", {}),
    (
        InboundArrived,
        "inbound_arrived",
        {"inbound_id": 42, "kind": "chat", "source": "user", "content": "hi"},
    ),
    (InboundCommitted, "inbound_committed", {"inbound_id": 42}),
    (TokenUsage, "token_usage", {"input_tokens": 1234, "output_tokens": 56, "reasoning_tokens": 0}),
    (LLMDone, "llm_done", {}),
    (
        ExecOutputChunk,
        "exec_output_chunk",
        {"item_id": "6.0", "content": "partial stdout", "keepalive": False},
    ),
    (LabelUpdated, "label_updated", {"label": "my label"}),
    (
        NoticePosted,
        "notice_posted",
        {"notice_id": 7, "priority": "P1", "title": "migration done", "task_id": 3},
    ),
    (NoticeResolved, "notice_resolved", {"notice_id": 7}),
    (TaskCreated, "task_created", {"task_id": 3}),
    (TaskUpdated, "task_updated", {"task_id": 3}),
]


def test_system_roles_includes_chat_streaming():
    assert "chat_start" in SYSTEM_ROLES
    assert "chat_delta" in SYSTEM_ROLES


def test_system_roles_includes_inbound_arrived_and_compact():
    assert "inbound_arrived" in SYSTEM_ROLES
    assert "compact_done" in SYSTEM_ROLES
    assert "code_delta" in SYSTEM_ROLES


def test_system_roles_includes_protocol_acks():
    assert "inbound_committed" in SYSTEM_ROLES
    assert "llm_done" in SYSTEM_ROLES
    assert "token_usage" in SYSTEM_ROLES


def test_global_roles_is_low_frequency_subset_of_system_roles():
    assert GLOBAL_ROLES < SYSTEM_ROLES

    high_frequency = {
        "chat_start",
        "chat_delta",
        "code_start",
        "code_delta",
        "reasoning_start",
        "reasoning_delta",
        "exec_start",
        "exec_output_chunk",
        "exec_output",
        "timeline_snapshot",
        "token_usage",
    }
    assert GLOBAL_ROLES.isdisjoint(high_frequency)

    assert {
        "agent_spawned",
        "agent_updated",
        "label_updated",
        "to_user_changed",
        "page_opened",
        "page_closed",
        "notice_posted",
        "notice_resolved",
        "task_created",
        "task_updated",
    } == GLOBAL_ROLES


@pytest.mark.parametrize(("cls", "expected_role", "extra"), FROZEN_WIRE)
def test_role_and_wire_shape_frozen(cls: type, expected_role: str, extra: dict[str, Any]) -> None:
    instance = cls(agent_id=7, **extra)
    assert instance.role == expected_role

    data = json.loads(instance.model_dump_json())
    assert data["agent_id"] == 7
    assert data["role"] == expected_role
    for k, v in extra.items():
        assert data[k] == v

    assert set(data.keys()) == {"agent_id", "role", *extra.keys()}


def test_role_registry_matches_event_union() -> None:
    from typing import get_args

    from base.events.live.projection import _ROLE_CLASSES, Event

    union = get_args(Event)[0]
    union_classes = set(get_args(union))
    registry_classes = {cls for cls, _ in _ROLE_CLASSES}
    assert union_classes == registry_classes, (
        f"Event union ({len(union_classes)} classes) and _ROLE_CLASSES "
        f"({len(registry_classes)} entries) drifted apart"
    )


def test_derived_role_sets_match_registry_flags() -> None:
    from base.events.live.projection import _ROLE_CLASSES, GLOBAL_ROLES, SYSTEM_ROLES

    assert frozenset(cls.model_fields["role"].default for cls, _ in _ROLE_CLASSES) == SYSTEM_ROLES
    assert (
        frozenset(cls.model_fields["role"].default for cls, g in _ROLE_CLASSES if g) == GLOBAL_ROLES
    )
    assert GLOBAL_ROLES <= SYSTEM_ROLES
