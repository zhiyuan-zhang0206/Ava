"""Terminate-exchange wire models — request, response, and the open-task hint.

The `ops.rpc_schemas` door re-exports these names.
"""

from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from base.agents.messages.envelope import reject_unnegotiated_caller, validate_writable_source
from ops.rpc_schemas.content import UserContent


class TerminateAgentRequest(BaseModel):
    """POST /api/agents/{id}/terminate request body — fully optional.

    `force` defaults to False for graceful termination. True directly kills the
    detached process and force-updates status when the agent cannot reach claim.

    `kill_all_shell_sessions` also kills every shell session the agent owns on
    its home machine (watchers included; `ava.ui.serve` page servers keep their
    own lifecycle), with no per-session notice. A graceful terminate of a live
    agent records the request on its terminate command and the home runtime
    kills the sessions right before the termination applies, after the agent's
    last step; a force terminate, or an agent that is already terminated, has
    them killed before the response — and a force sweeps them again once its
    host observes the agent's work ended. Without it, sessions are left alone.
    A terminated agent is otherwise an ordinary terminated agent — any new
    message may resurrect it
    (docs/decisions/2026-09-27-terminate-has-no-closed-state.md).

    `source` defaults to "user"; SDK paths pass f"agent:{my_id}". Claim
    includes this source in the lifecycle marker shown to the agent.

    `message`, when present, is queued as chat immediately before the terminate
    inbound. Lifecycle acceptance claims only the terminate row, so the chat
    remains pending for the next resurrection without another LLM turn.
    """

    force: bool = Field(default=False)
    kill_all_shell_sessions: bool = Field(default=False)
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


class ShellSessionsKill(BaseModel):
    """What `kill_all_shell_sessions` did for one terminate request.

    `when="now"`: the kill already ran on the agent's home machine — a force
        terminate, or an agent that was already terminated. `killed` lists the
        session ids it killed, ascending; empty means the agent had no shell
        session there. A force also sweeps again once its host observes the
        agent's work ended, catching a session its last step created after
        this kill; that sweep is not in this response.
    `when="at_exit"`: a graceful terminate of a live agent. The request is
        durable on its terminate command; the home runtime kills every session
        right before the termination applies, after the agent's last step.
        `killed` is empty — the set is fixed only at exit.
    """

    when: Literal["now", "at_exit"]
    killed: list[int] = Field(default_factory=list[int])


class TerminateAgentResponse(BaseModel):
    """POST /api/agents/{id}/terminate response.

    `enqueued`: termination accepted, including hosted force. Actual work may
        still be draining; this result does not prove exit.
    `already_terminated`: agent was already dead. Graceful termination is a
        no-op beyond a requested shell-session kill. Hosted force instead
        returns enqueued until its exact original host can prove quiescence;
        metadata status alone is not exit evidence.

    `open_tasks`: the still-open tasks the agent owned as it went down — an
        advisory hint, null when it owned none or the hint read failed.

    `shell_sessions`: what the shell-session kill did — set when
        `kill_all_shell_sessions` was requested, None otherwise. A requested
        kill answered with None means the home runner predates the option
        (version skew): nothing was killed.
    """

    status: Literal["enqueued", "already_terminated"]
    open_tasks: OpenTasksHint | None = None
    shell_sessions: ShellSessionsKill | None = None
