"""Freeze the wire format of `events.Event`.

Purpose: pin the "class ↔ role string" mapping + serialization shape in one shot.
Renaming any role (e.g. `Literal["code_delta"]` → `"code_chunk"`) goes red
immediately — class-construction unit tests are "symmetric" (producer and consumer
changed together in the same PR), so they can't catch role-literal drift; but the
role string is the UI tailer's discriminator and the anchor for eyeball-matching in
logs when hunting bugs — it must not change silently.

Only the wire literals are tested — pydantic behaviour (discriminator dispatch,
unknown role rejection) is already tested in `test_render.py`.
"""

import json
from typing import Any

import pytest

from shared.live_events import (
    GLOBAL_ROLES,
    SYSTEM_ROLES,
    Cancelled,
    ChatDelta,
    ChatStart,
    ClusterUpdateStarted,
    CodeDelta,
    CodeStart,
    CompactDone,
    CompactRequest,
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
)

# Frozen list of (class, expected role, extra fields beyond agent_id/role).
# When adding a role, append it here — forget and the new Event variant is unpinned.
FROZEN_WIRE: list[tuple[type, str, dict[str, Any]]] = [
    (ChatStart, "chat_start", {"item_id": "5.0"}),
    (ChatDelta, "chat_delta", {"item_id": "5.0", "content": "hello"}),
    (CompactRequest, "compact_request", {"content": "[compact requested, 5 chars]"}),
    (CompactDone, "compact_done", {}),
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
    (
        ClusterUpdateStarted,
        "cluster_update_started",
        {"kind": "rollout", "origin": "user"},
    ),
]


def test_system_roles_includes_chat_streaming():
    """chat_start / chat_delta go through the system channel, same as reasoning / code."""
    assert "chat_start" in SYSTEM_ROLES
    assert "chat_delta" in SYSTEM_ROLES


def test_system_roles_includes_inbound_arrived_and_compact():
    assert "inbound_arrived" in SYSTEM_ROLES
    assert "compact_done" in SYSTEM_ROLES
    assert "code_delta" in SYSTEM_ROLES


def test_system_roles_includes_protocol_acks():
    """Protocol-layer ACK events — the frontend depends on these to trigger reloads;
    a typo in the role name causes a silent frontend break."""
    assert "inbound_committed" in SYSTEM_ROLES
    assert "llm_done" in SYSTEM_ROLES
    assert "token_usage" in SYSTEM_ROLES


def test_global_roles_is_low_frequency_subset_of_system_roles():
    """GLOBAL_ROLES (the /api/system broadcast) must stay a strict subset of
    SYSTEM_ROLES and must NOT carry any high-frequency per-turn role. The
    broadcast fans out to every connected client for every agent, so a
    token-level role here re-introduces the N-clients x M-agents blowup the
    per-agent /api/agents/{id}/system split was built to kill."""
    assert GLOBAL_ROLES < SYSTEM_ROLES  # strict subset

    # The per-turn streaming roles are the blowup source — they belong only on
    # the per-agent channel, never on the broadcast.
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

    # The cross-agent fleet views plus the cluster-update takeover are the only
    # consumers of the broadcast: the sidebar list (spawned / updated / label),
    # the pages popover (page open / close), the FYI notice feed (notice posted /
    # resolved), and the task board (task created / updated).
    assert {
        "agent_spawned",
        "agent_updated",
        "label_updated",
        "page_opened",
        "page_closed",
        "notice_posted",
        "notice_resolved",
        "task_created",
        "task_updated",
        "cluster_update_started",
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

    # No extra fields — consumers parse by fixed field names, producers must not silently leak new keys
    assert set(data.keys()) == {"agent_id", "role", *extra.keys()}


def test_role_registry_matches_event_union() -> None:
    """The static `Event` union and the `_ROLE_CLASSES` registry must name the
    same class set.

    The registry drives SYSTEM_ROLES / GLOBAL_ROLES / EVENT_ADAPTER (R2-C), so
    a role added to one side but not the other would silently split the
    live-projection contract — the union is the statically-typed surface, the
    registry is the runtime one. This guard keeps them pinned together."""
    from typing import get_args

    from shared.live_events import _ROLE_CLASSES, Event

    union = get_args(Event)[0]  # Annotated[Union[...], Field] -> the union
    union_classes = set(get_args(union))
    registry_classes = {cls for cls, _ in _ROLE_CLASSES}
    assert union_classes == registry_classes, (
        f"Event union ({len(union_classes)} classes) and _ROLE_CLASSES "
        f"({len(registry_classes)} entries) drifted apart"
    )


def test_derived_role_sets_match_registry_flags() -> None:
    """SYSTEM_ROLES / GLOBAL_ROLES are derived from `_ROLE_CLASSES`; the flags
    in the registry are the only free variable, so pin them here."""
    from shared.live_events import _ROLE_CLASSES, GLOBAL_ROLES, SYSTEM_ROLES

    assert frozenset(cls.model_fields["role"].default for cls, _ in _ROLE_CLASSES) == SYSTEM_ROLES
    assert (
        frozenset(cls.model_fields["role"].default for cls, g in _ROLE_CLASSES if g) == GLOBAL_ROLES
    )
    assert GLOBAL_ROLES <= SYSTEM_ROLES
