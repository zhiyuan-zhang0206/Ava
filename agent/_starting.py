"""Enter the starting state: UPDATE agents_meta status 'allocated' → 'starting'
before heavy imports — the entry into the boot stage.

The boot stage is the window between this process taking ownership of its row
and the run loop going live: the row sits 'starting' for the whole stage (heavy
import + run-loop init). `enter_starting_state` opens it here;
`agent/db.py:leave_starting_state` ('starting' -> 'running', at the run loop's
first claim node) closes it. Distinct from the run loop's *claim node*
(`agent/graph/_claim.py`), which claims inbound work batches, not the row.

This module imports NOTHING from langgraph/langchain — its sole purpose is to
enter the starting state as early as possible so the gateway's spawn poll
(`_wait_for_status_to_leave_allocated`) returns quickly, reducing spawn latency
from ~5s to ~1-2s. Must be callable before `agent.loop` triggers the full
langgraph import chain.

Each step here closes a `_boot_timing` phase. Nothing in this file reads those
marks, but the boot watchdog armed in `agent/__main__.py` does: they are the only
evidence that a pre-claim boot is moving, and reaching one is what buys the child
more patience. Keep marking any step added here, or the watchdog will judge the
new step's duration to be a stall.
"""

from __future__ import annotations

import os
import sys

import shared.db
from agent import _boot_timing
from shared.agents import AgentStatus
from shared.config import settings
from shared.live_announce import publish_agent_updated_sync, publish_page_closed_sync
from shared.machine import machine_name
from shared.migrations import (
    CodeBehindSchema,
    SchemaVersionMismatch,
    assert_schema_current,
)


def enter_starting_or_die_on_stale_schema(agent_id: int) -> None:
    """Schema-gate, then enter the starting state — in one early-boot step.

    Verify the central DB schema matches this code BEFORE flipping the row
    'allocated' -> 'starting'. Either direction of schema mismatch fails here —
    the agent code must exactly match the DB schema:

    - `CodeBehindSchema` (applied > required): local code is stale; this
      agent-runner missed an update fan-out. The host can self-heal by
      catching its code up to the cluster.
    - `SchemaVersionMismatch` (applied < required): this checkout carries
      pending migrations the DB has not seen — typically the prod checkout
      got switched to a feature branch. The host cannot self-heal (the code
      itself is the problem); it must return the checkout to `main`.

    On either mismatch, leave the row unclaimed, mark it 'terminated', emit a
    clear "schema mismatch" line to stderr, and let the error propagate so the
    process exits.

    Ordering matters: a 'starting' row carries our pid and only leaves that
    state when the process reaches the run loop (-> 'running') or something
    reaps it. A boot that cannot proceed must therefore fail BEFORE entering
    'starting', never advertising it for a pid that is about to die. Marking the
    still-'allocated' row 'terminated' leaves a clean, visible state instead
    of a lingering 'allocated'; the host self-heals its code and the agent can
    then be resurrected.

    The run loop re-checks the schema later (before checkpointer setup); this
    earlier gate exists specifically to protect the 'allocated' -> 'starting'
    transition.
    """
    try:
        assert_schema_current(settings.data_plane.db_url)
    except (CodeBehindSchema, SchemaVersionMismatch) as exc:
        print(  # noqa: T201 — early-boot stderr diagnostic, before logging is set up
            f"  [schema mismatch] agent {agent_id} boot rejected: {exc}",
            file=sys.stderr,
        )
        _mark_allocated_terminated(agent_id)
        raise
    _boot_timing.mark("schema_check")
    enter_starting_state(agent_id)


def _mark_allocated_terminated(agent_id: int) -> None:
    """Flip our own still-'allocated' row to 'terminated' before exiting on a
    boot failure that happened before entering 'starting'. Guarded
    `WHERE status = 'allocated'` so it never clobbers a row some other process
    legitimately took.

    Stamps `termination_source='launch-confirm'` in the SAME statement as the
    status flip — the invariant `ops/controllers/resurrect.py` documents is that
    EVERY terminated-write stamps a source, because a NULL source is permanently
    unresurrectable, so a corpse left here without one silently strands whatever
    inbound work the agent still had queued.

    Why 'launch-confirm' for BOTH callers (schema mismatch and placement
    mismatch): this write IS the cause of a launch-confirm failure, not merely
    something adjacent to one. `_wait_for_status_to_leave_allocated` treats
    'terminated' + NULL pid — exactly what this writes — as "the boot was
    rejected before claiming", and the launcher would itself have stamped
    'launch-confirm' had it won the race to the row. It only loses that race on
    the spawn path, whose force-terminate is guarded `WHERE status='allocated'`
    and so matches 0 rows once we have already flipped it. Same event, two ends.

    Both callers also WANT the resurrect that the stamp buys, for the same
    reason: each is a condition that clears without touching this row. A
    placement mismatch names another machine, and the resurrect scan is
    machine-scoped, so the corpse is claimable only by the host the row actually
    names — resurrecting is what puts the agent on its correct host. A schema
    mismatch clears the moment the host catches its code up (the remedy
    `_wait_for_status_to_leave_allocated` names: let the watchdog run `ava
    update`). While either condition persists the boot fails again and is
    re-stamped, so the worst case is a backoff-spaced retry, each a loud WARN —
    which is the shape `CrashResurrectController` is built for (no hard cap,
    precisely so nothing is permanently stranded). A bounded, loud retry beats a
    corpse that is silently unrecoverable forever.
    """
    with shared.db.connect() as conn, conn.cursor() as cur:
        # Capture open page names BEFORE the status flip — the
        # cascade_close_agent_pages trigger closes the page rows on
        # 'terminated', after which the open-page query matches nothing.
        # An 'allocated' row can hold open pages from a previous incarnation
        # (resurrect's cascade_open reopens them at the flip); the frontend
        # popover needs the PageClosed events to drop them.
        cur.execute(
            "SELECT name FROM agent_pages WHERE agent_id = %s AND closed_at IS NULL",
            (agent_id,),
        )
        page_names = [r[0] for r in cur.fetchall()]
        cur.execute(
            "UPDATE agents_meta SET status = 'terminated', "
            "termination_source = 'launch-confirm' "
            "WHERE id = %s AND status = 'allocated'",
            (agent_id,),
        )
        transitioned = cur.rowcount == 1
        conn.commit()
        publish_agent_updated_sync(conn, agent_id)
    # Only a real transition closes the pages (the cascade trigger) — a row
    # another process took (rowcount 0) keeps its pages open, so publishing
    # PageClosed would wrongly drop live popover entries.
    if transitioned:
        for page_name in page_names:
            publish_page_closed_sync(agent_id, page_name)


def enter_starting_state(agent_id: int) -> None:
    """UPDATE agents_meta.status from 'allocated' to 'starting' — this process
    takes ownership of its row (the boot-stage entry; paired with
    `agent/db.py:leave_starting_state`). Writes the R1 liveness lease
    (`lease_expires_at`, TTL `shared.deploy_timing.AGENT_LEASE_TTL_S`) in the
    same UPDATE — the claim is the lease's birth; the run loop renews it.

    Zero langgraph imports on purpose: called from `agent/__main__.py`
    before importing `agent.loop`, so the gateway's spawn poller returns
    in ~1-2s instead of waiting for the full langgraph import + asyncio
    event loop start.

    Validates `agents_meta.machine == machine_name()` (this host = the
    placement machine recorded on the row); mismatch marks the
    still-'allocated' row 'terminated' (same as the schema gate — the
    launcher's confirm poll fails fast on 'terminated' with no pid instead
    of waiting out its timeout) and raises — in a multi-host deployment,
    starting the process on the wrong host is a placement bug; should
    fail-loud rather than silently continue. The machine column is written
    on the create path (`spawn_agent`); this only validates.

    0-row raises — possible reasons: agent_id does not exist / not in
    allocated state (already starting means duplicate start; already
    terminated should go through resurrect_agent path) / taken by
    another process.
    """
    local_machine = machine_name()
    with shared.db.connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT machine FROM agents_meta WHERE id = %s", (agent_id,))
        row = cur.fetchone()
        if row is None:
            raise RuntimeError(
                f"agent --agent-id {agent_id}: agents_meta row does not exist. "
                "Create first via spawn_agent / resurrect_agent."
            )
        row_machine = row[0]
        if row_machine != local_machine:
            _mark_allocated_terminated(agent_id)
            raise RuntimeError(
                f"agent --agent-id {agent_id}: placement mismatch — "
                f"agents_meta.machine={row_machine!r} but this host machine_name()={local_machine!r}. "
                "Process started on wrong host (gateway routing bug or manual mis-launch)."
            )
        _boot_timing.mark("placement_check")
        from shared.deploy_timing import AGENT_LEASE_TTL_S

        cur.execute(
            "UPDATE agents_meta SET status = %s, pid = %s, started_at = now(), "
            "lease_expires_at = now() + make_interval(secs => %s) "
            "WHERE id = %s AND status = %s",
            (AgentStatus.STARTING, os.getpid(), AGENT_LEASE_TTL_S, agent_id, AgentStatus.ALLOCATED),
        )
        if cur.rowcount != 1:
            raise RuntimeError(
                f"agent --agent-id {agent_id}: agents row does not exist or not in 'allocated' state "
                f"(rowcount={cur.rowcount}). Create first via spawn_agent / resurrect_agent."
            )
        conn.commit()
        publish_agent_updated_sync(conn, agent_id)
