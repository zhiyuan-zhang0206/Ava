"""Placement and termination boundaries for a queued resurrection."""

from uuid import UUID

import psycopg
from pydantic import ValidationError

from base.agents import AgentNotFound, MachinePaused, ResurrectError, ResurrectRefused
from base.agents.incarnation.resources import ResourceBirth, ResourceShapeError, decode_resources
from base.log import logger
from base.native_process.runtime_incarnation import RuntimeIncarnation
from ops.cluster_rpc import ClusterOpFailed
from ops.rpc_schemas import OpFailure

# The ops server's wire form of an exception it returns as a failed result.
_REMOTE_REFUSAL = f"{ResurrectRefused.__name__}: "


class ResurrectSettlementDeferredError(ResurrectError):
    """The original hosted lifecycle command has not yet settled."""


class ResurrectTriggerStaleError(ResurrectError):
    """The exact pending wake no longer qualifies; the local op returns a no-op."""


def hosted_resurrection_target(
    agent_id: int,
    *,
    kind: str | None,
    generation: UUID | None,
    owner: UUID | None,
    pid: int | None,
    resources: object,
    unowned_termination: int | None,
) -> RuntimeIncarnation | None:
    """Require retained hosted authority, proven non-admission or an unowned end.

    A retained hosted identity resumes that incarnation; its settled command
    and resource set close it at the successor's admission. Every other
    accepted row has all runtime identity fields empty and resurrects as a
    fresh hosted birth, returned as None, in two cases:

    - the runtime never admitted it: the fresh-INSERT birth marker is still
      unconsumed, so no predecessor allocation exists;
    - `unowned_termination` names this life's force receipt
      (`base.agents.incarnation.lifecycle_acceptance.record_unowned_termination`): this runtime
      ended the row while no incarnation owned it, and its own lifecycle had
      left it unowned. Resurrection restores the state that force ended;
      admission still decides the successor.

    NULL resources are unknown, not proof of either. Historical process/unknown
    rows, incomplete identities, unowned rows without a receipt (including
    legacy ones this runtime ended) and resources the current model cannot
    decode need explicit cutover reconciliation.
    """
    try:
        state = None if resources is None else decode_resources(resources)
    except ResourceShapeError:
        raise ResurrectRefused("runtime_cutover_required") from None
    if (kind, generation, owner, pid) == (None,) * 4 and (
        isinstance(state, ResourceBirth) or unowned_termination is not None
    ):
        return None
    if kind != "hosted" or generation is None or owner is None or pid is not None:
        raise ResurrectRefused("runtime_cutover_required")
    return RuntimeIncarnation(agent_id, generation, owner)


def lock_active_home_machine(cur: psycopg.Cursor, agent_id: int) -> str:
    """Share the pause latch before metadata/inbound locks or budget writes."""
    cur.execute("SELECT machine FROM agents_meta WHERE id = %s", (agent_id,))
    agent_row = cur.fetchone()
    if agent_row is None:
        raise AgentNotFound(f"agent {agent_id} does not exist")
    home_machine = agent_row[0]
    if not isinstance(home_machine, str):
        raise ResurrectTriggerStaleError("resurrection target has no registered placement")
    cur.execute("SELECT paused_at FROM machines WHERE name = %s FOR SHARE", (home_machine,))
    machine_row = cur.fetchone()
    if machine_row is not None and machine_row[0] is not None:
        raise MachinePaused(
            f"agent {agent_id} home machine {home_machine!r} is paused; "
            "resume it before resurrecting"
        )
    return home_machine


def resurrect_refusal_reason(exc: BaseException) -> str | None:
    """The durable refusal reason carried by an auto-resurrect failure, if any.

    In process the op raises `ResurrectRefused`; forwarded to the home runner,
    the ops server returns it as a failed result naming that exception.
    """
    if isinstance(exc, ResurrectRefused):
        return exc.reason
    if isinstance(exc, ClusterOpFailed):
        try:
            error = OpFailure.model_validate(exc.result).error
        except ValidationError:
            return None
        if error.startswith(_REMOTE_REFUSAL):
            return error.removeprefix(_REMOTE_REFUSAL)
    return None


def report_auto_resurrect_failure(agent_id: int, exc: Exception) -> None:
    """Report a swallowed auto-resurrect failure; the triggering inbound stays queued.

    A refusal (e.g. `runtime_cutover_required`) never clears by itself, so it
    is a WARNING naming the reason. Other failures may be transient: INFO.
    """
    reason = resurrect_refusal_reason(exc)
    if reason is not None:
        logger.warning(
            "auto-resurrect of agent {agent_id} refused ({reason}); its inbound stays "
            "queued until an operator resolves the refusal",
            event="auto_resurrect_refused",
            agent_id=agent_id,
            reason=reason,
        )
        return
    logger.opt(exception=exc).info(
        "auto-resurrect of agent {agent_id} failed; inbound queued, manual resurrect "
        "will pick it up",
        event="auto_resurrect_failed",
        agent_id=agent_id,
    )
