"""Placement and termination boundaries for a queued resurrection."""

from uuid import UUID

import psycopg
from pydantic import ValidationError

from ops.cluster_rpc import ClusterOpFailed
from ops.rpc_schemas import OpFailure
from shared.agents import AgentNotFound, MachinePaused, ResurrectError, ResurrectRefused
from shared.incarnation_resources import ResourceBirth, ResourceShapeError, decode_resources
from shared.log import logger
from shared.runtime_incarnation import RuntimeIncarnation

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
) -> RuntimeIncarnation | None:
    """Require retained hosted authority or proven non-admission.

    A retained hosted identity resumes that incarnation; its settled command
    and resource set close it at the successor's admission. A row the runtime
    never admitted (every runtime identity field empty and the fresh-INSERT
    birth marker still unconsumed) resurrects as a fresh hosted birth, returned
    as None: no predecessor allocation exists. NULL resources are unknown, not
    proof of non-admission. Historical process/unknown rows, incomplete
    identities and resources the current model cannot decode need explicit
    cutover reconciliation.
    """
    try:
        state = None if resources is None else decode_resources(resources)
    except ResourceShapeError:
        raise ResurrectRefused("runtime_cutover_required") from None
    if isinstance(state, ResourceBirth) and (kind, generation, owner, pid) == (None,) * 4:
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
