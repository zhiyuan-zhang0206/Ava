"""Terminate-exchange wire models — request, response, and the open-task hint.

Split out of `ops/rpc_schemas.py` when that file crossed the per-file line
ceiling. `ops.rpc_schemas` re-exports these names, so every existing import
path stays valid.
"""

from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from ops.rpc_content import UserContent
from shared.envelope import reject_unnegotiated_caller, validate_writable_source


class TerminateAgentRequest(BaseModel):
    """POST /api/agents/{id}/terminate request body — fully optional.

    `force` defaults to False for graceful termination. True directly kills the
    detached process and force-updates status when the agent cannot reach claim.

    `final` closes the agent — the closure marker (`agents_meta.closed_at`)
    means "never auto-resurrect": every automatic resurrection path skips it
    and its queued work dead-letters on the existing thresholds; only an
    explicit manual resurrect reopens it. Stamped even when the terminate lands
    on an already-terminated agent (metadata-only mark; the backfill route).

    `source` defaults to "user"; SDK paths pass f"agent:{my_id}". Claim
    includes this source in the lifecycle marker shown to the agent.

    `message`, when present, is queued as chat immediately before the terminate
    inbound. Lifecycle acceptance claims only the terminate row, so the chat
    remains pending for the next resurrection without another LLM turn.
    """

    force: bool = Field(default=False)
    final: bool = Field(default=False)
    source: str = Field(default="user", min_length=1, max_length=64)
    message: UserContent | None = None

    @field_validator("source")
    @classmethod
    def _check_source(cls, value: str) -> str:
        # Lifecycle audit reasons include opaque legacy values such as
        # machine-pause; they are not chat envelope source identifiers.
        reject_unnegotiated_caller(value)
        return value

    @model_validator(mode="after")
    def _validate_message_source(self) -> "TerminateAgentRequest":
        if self.message is not None:
            validate_writable_source(self.source)
        return self


class OpenTaskRow(BaseModel):
    """One open task a terminating agent still owns; `updated_at` is ISO-8601.

    The terminate hint shows at most five, most recently updated first."""

    id: int
    title: str
    status: Literal["in_progress"]
    updated_at: str


class OpenTasksHint(BaseModel):
    """The open tasks (in_progress) a terminating agent still owns.

    Advisory only — termination proceeds either way. `tasks` holds at most the
    five most recently updated rows; `more` counts the ones beyond those."""

    count: int
    tasks: list[OpenTaskRow]
    more: int


class TerminateAgentResponse(BaseModel):
    """POST /api/agents/{id}/terminate response.

    `enqueued`: termination accepted, including hosted force. Actual work may
        still be draining; this result does not prove exit.
    `already_terminated`: agent was already dead. Graceful termination is a
        no-op. Hosted force instead returns enqueued until its exact original
        host can prove quiescence; metadata status alone is not exit evidence.

    `open_tasks`: the still-open tasks the agent owned as it went down — an
        advisory hint, null when it owned none or the hint read failed.

    `closed`: the agent's closure state after this request — True when `final`
        was requested (the marker is stamped in the same transaction as the
        termination intent; the already-terminated backfill marks idempotently)
        or when the marker was already set; False otherwise; None only when the
        reporting runner predates the field (version skew). Makes
        `terminate --final` — including its already-terminated backfill form —
        verifiable from the response alone.
    """

    status: Literal["enqueued", "already_terminated"]
    open_tasks: OpenTasksHint | None = None
    closed: bool | None = None
