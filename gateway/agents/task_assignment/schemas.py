"""Guarded compound task/agent acceptance and independent launch observations."""

from typing import Annotated
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from base.agents.tasks.model import Task
from base.agents.tasks.priority import MAX_REMIND_INTERVAL_SECONDS, Priority
from ops.rpc_schemas import SpawnedAgent

PositiveId = Annotated[int, Field(strict=True, gt=0)]
ReminderSeconds = Annotated[int, Field(strict=True, ge=1, le=MAX_REMIND_INTERVAL_SECONDS)]


class AssignmentTaskIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str
    description: str
    parent: PositiveId
    priority: Priority = Priority.P2
    remind_interval_seconds: ReminderSeconds | None = None


class AssignmentAgentIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    label: str | None = None
    machine: str | None = None
    config: dict[str, object] | None = None


class TaskAssignmentIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    actor_agent_id: PositiveId
    task: AssignmentTaskIn
    agent: AssignmentAgentIn


class TaskAssignmentResult(BaseModel):
    """Immutable business acceptance; Task is its original snapshot."""

    model_config = ConfigDict(extra="forbid")
    task: Task
    agent_id: PositiveId
    launch_attempt_id: UUID


class TaskAssignmentAccepted(TaskAssignmentResult):
    """Original pair plus current launch observation; neither proves execution."""

    launch: SpawnedAgent | None = None
    launch_failure: str | None = None
    retry_launch_path: str | None = None
