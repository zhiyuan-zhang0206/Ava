"""Atomic compound task and agent acceptance for the Fleet SDK."""

from typing import Any

import ava.self
from ava.gateway_client import transport
from ava.sdk_surface.agent_identity import require_lease_free_agent_id
from base.agents.tasks.model import Task, validate_task_snapshot
from base.agents.tasks.priority import validate_priority, validate_remind_interval_seconds
from base.api_contracts.idempotency import PRINCIPAL_SCOPE


def create_and_assign_guarded(
    title: str,
    description: str,
    *,
    parent: int,
    preset: str | None,
    label: str | None,
    config_overlay: dict[str, Any] | None,
    machine: str | None,
    remind_interval_seconds: int | None,
    priority: str,
    operation_key: str,
) -> tuple[Task, int]:
    """Return the accepted original pair; launch/execution are separate owners."""
    actor = require_lease_free_agent_id()
    if isinstance(parent, bool) or not isinstance(parent, int):
        raise TypeError("parent must be an integer")
    if parent <= 0:
        raise ValueError("parent must be positive")
    validate_priority(priority)
    if remind_interval_seconds is not None:
        if isinstance(remind_interval_seconds, bool):
            raise TypeError("remind_interval_seconds must be an integer")
        validate_remind_interval_seconds(remind_interval_seconds)
    overlay = dict(config_overlay) if config_overlay else {}
    if preset is not None:
        if "preset" in overlay:
            raise ValueError("preset given twice; pass it only as preset")
        overlay["preset"] = preset
    body = {
        "actor_agent_id": actor,
        "task": {
            "title": title,
            "description": description,
            "parent": parent,
            "priority": priority,
            "remind_interval_seconds": remind_interval_seconds,
        },
        "agent": {
            "label": label,
            "machine": machine if machine is not None else ava.self.SELF_MACHINE_NAME,
            "config": overlay,
        },
    }
    response = transport.post(
        "/api/keyed/v1/task-assignments",
        body,
        idempotency_key=operation_key,
        idempotency_scope=PRINCIPAL_SCOPE,
    )
    transport.raise_from_response(response)
    result = response.json()
    snapshot = validate_task_snapshot(result["task"])
    agent_id = result["agent_id"]
    if isinstance(agent_id, bool) or not isinstance(agent_id, int):
        raise TypeError("accepted agent id must be an integer")
    if agent_id <= 0:
        raise ValueError("accepted agent id must be positive")
    return Task(**snapshot), agent_id
