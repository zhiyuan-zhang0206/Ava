"""Claim an agent row before importing the heavy runtime.

The bootstrap process claims an unowned ``idling`` row directly into
``running``. Status deliberately does not expose a separate boot stage: the
pid, start time, and lease written by this CAS carry the ownership facts.

This module imports nothing from langgraph/langchain, so the launcher can
confirm the child as soon as its row has a pid instead of waiting for the full
runtime import chain.
"""

from __future__ import annotations

import os
import sys

from agent import _boot_timing
from shared.agents import AgentStatus
from shared.config import settings
from shared.db_transaction import write_transaction
from shared.live_announce import publish_agent_updated_sync, publish_page_closed_sync
from shared.machine import machine_name
from shared.migrations import (
    CodeBehindSchema,
    SchemaVersionMismatch,
    assert_schema_current,
)
from shared.runtime_incarnation import bind_process_incarnation, new_process_incarnation


def claim_agent_row_or_die_on_stale_schema(agent_id: int) -> None:
    """Schema-gate, then claim this process's row in one early boot step.

    A schema or placement failure happens before the claim, so the guarded
    terminal write cannot clobber a concurrent successful claimant. Stamping
    ``launch-confirm`` keeps the involuntary failure eligible for the existing
    crash-resurrect backoff path.
    """
    try:
        assert_schema_current(settings.data_plane.db_url)
    except (CodeBehindSchema, SchemaVersionMismatch) as exc:
        print(  # noqa: T201 — early-boot stderr diagnostic, before logging is set up
            f"  [schema mismatch] agent {agent_id} boot rejected: {exc}",
            file=sys.stderr,
        )
        _mark_preclaim_terminated(agent_id)
        raise
    _boot_timing.mark("schema_check")
    claim_agent_row(agent_id)


def _mark_preclaim_terminated(agent_id: int) -> None:
    """Mark an unclaimed row terminated and close its show() pages after boot rejection."""
    with write_transaction() as conn, conn.cursor() as cur:
        # Capture only agent-owned pages before the terminal transition closes them.
        cur.execute(
            "SELECT name FROM agent_pages "
            "WHERE agent_id = %s AND closed_at IS NULL AND expired_at IS NULL "
            "AND serve_dir IS NULL",
            (agent_id,),
        )
        page_names = [r[0] for r in cur.fetchall()]
        cur.execute(
            "UPDATE agents_meta SET status = 'terminated', "
            "termination_source = 'launch-confirm' "
            "WHERE id = %s AND status = 'idling' AND pid IS NULL",
            (agent_id,),
        )
        transitioned = cur.rowcount == 1
        conn.commit()
        publish_agent_updated_sync(conn, agent_id)
    if transitioned:
        for page_name in page_names:
            publish_page_closed_sync(agent_id, page_name)


def claim_agent_row(agent_id: int) -> None:
    """Atomically claim an unowned row as running and grant its first lease.

    ``status='idling' AND pid IS NULL`` is the single-winner boundary. A
    second child sees zero updated rows and fails instead of sharing an agent
    identity with the process that already owns it.
    """
    local_machine = machine_name()
    incarnation = new_process_incarnation(agent_id)
    with write_transaction() as conn, conn.cursor() as cur:
        # A guarded auto-resurrect transaction may still be committing the row
        # when its child starts. Lock first so this process validates that final
        # committed state rather than a stale pre-resurrection snapshot.
        cur.execute(
            "SELECT machine FROM agents_meta WHERE id = %s FOR UPDATE",
            (agent_id,),
        )
        row = cur.fetchone()
        if row is None:
            raise RuntimeError(
                f"agent --agent-id {agent_id}: agents_meta row does not exist. "
                "Create first via spawn_agent or resurrect_agent."
            )
        row_machine = row[0]
        if row_machine != local_machine:
            conn.rollback()
            _mark_preclaim_terminated(agent_id)
            raise RuntimeError(
                f"agent --agent-id {agent_id}: placement mismatch — "
                f"agents_meta.machine={row_machine!r} but this host machine_name()={local_machine!r}. "
                "Process started on the wrong host."
            )
        _boot_timing.mark("placement_check")
        from shared.deploy_timing import AGENT_LEASE_TTL_S

        cur.execute(
            "UPDATE agents_meta SET status = %s, pid = %s, started_at = now(), "
            "lease_expires_at = now() + make_interval(secs => %s), "
            "runtime_generation = %s, runtime_owner = %s, runtime_kind = 'process', "
            "runtime_protocol_version = 0 "
            "WHERE id = %s AND status = %s AND pid IS NULL "
            "AND (runtime_kind IS NULL OR runtime_kind = 'process')",
            (
                AgentStatus.RUNNING,
                os.getpid(),
                AGENT_LEASE_TTL_S,
                incarnation.generation,
                incarnation.owner,
                agent_id,
                AgentStatus.IDLING,
            ),
        )
        if cur.rowcount != 1:
            raise RuntimeError(
                f"agent --agent-id {agent_id}: agents row is no longer an unclaimed idling row "
                f"(rowcount={cur.rowcount}); another process or lifecycle operation won the race."
            )
        from agent.lifecycle_observe import observe_process_admission

        observe_process_admission(conn, incarnation)
        conn.commit()
        bind_process_incarnation(incarnation)
        publish_agent_updated_sync(conn, agent_id)
