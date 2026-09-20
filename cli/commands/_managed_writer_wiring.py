"""The managed-writer wiring: coordinator-side call positions of the W chain.

The enable point (`cli.commands._managed_writer_mode`) resolves one mode decision
per rollout; these are the rollout steps that consume it and drive the
publication seats (`cli.commands._update_publication`) on the coordinator (task
#4128):

- `_begin_managed_writer_publication` (E2-b) -- the P1 journal position, after
  the prepare gate and before the first stop effect: under an `active` decision
  the dispatch chain (task #4129, channel B) reads the sealed release context,
  gathers every registered unit's prepared facts, seals the all-unit prepared
  plan, journals it through the P1 seat and requires every unit's local
  validation acknowledgement -- so agent births freeze and the current
  publication is preserved until P5 completes. Any refusal in that chain aborts
  the rollout before it stops anything. `off` / `blocked` skip it untouched.
- `_collect_managed_writer_publication` (E2-c) -- the post-Phase-B, pre-commit
  collection step: under an `active` decision the completed units' post-stop
  facts are gathered across the fleet and adopted into the pending journal
  before P5 publishes. The gathering channel (the collector) is not connected
  yet (task #4129), so the step refuses explicitly today rather than let an
  `active` rollout publish without its collection. `off` / `blocked` skip it
  untouched.
- `_commit_managed_writer_publication` (E2-a) -- the post-Phase-B step: under an
  `active` decision, publish the completed pending publication through the P5
  commit seat. `off` / `blocked` skip it untouched, so the pre-wiring rollout
  behavior is unchanged; a call with no recorded decision skips with a visible
  beacon (it can only mean the position ran outside the rollout's read point).
  A set that cannot be published refuses fail-closed, leaving the pending
  journal for checked recovery (`ava cluster recover-pending`).

All three call positions are wired (begin E2-b, collect E2-c, commit E2-a).
The prepared-plan chain behind the begin step is connected (task #4129, channel
B); the collection channel behind the collect step is not yet, so that step
still refuses explicitly under an `active` decision -- the commit seat is
imported and called, but only a rollout whose begin and collect succeeded can
reach it, so no half-open activation window exists in code. The inertness pin
(`tests/cli/test_update_publication.py::test_the_seat_has_no_unnamed_production_callsite`)
names this module and the dispatch chain as conscious production exceptions
beside the enable point.

Each step imports its seats lazily, inside the `active` check: nothing reaches
them unless the rollout actually entered the managed-writer mode.
"""

from __future__ import annotations

import sys

from shared.rollout_telemetry import stage as _stage_telemetry


def _begin_managed_writer_publication(target_sha: str | None) -> int:
    """Journal the managed-writer activation (P1 coordinator step).

    The rollout's begin position of the managed-writer chain (task #4128,
    E2-b): under an `active` decision the dispatch chain (task #4129, channel
    B) reads the sealed release context, gathers each registered unit's
    prepared facts, seals the all-unit prepared plan, opens the P1 journal and
    requires every unit's local validation acknowledgement before the rollout
    stops anything -- so agent births freeze and the current publication is
    preserved until P5 completes. `off` / `blocked` leave the seat untouched,
    and a call with no recorded decision skips with the same visible beacon as
    the commit step.

    A refusal anywhere in the chain -- a missing or mismatched release context,
    a lease this process does not own, a gather or seat refusal, a unit that
    does not acknowledge its copy of the plan -- is printed and turns into
    exit code 1; the rollout then aborts before the first stop effect, and a
    journal already opened stays for checked recovery (`ava cluster
    recover-pending`). Infrastructure failures propagate unchanged. Returns
    the step's exit code: 0 for a clean skip or an open journal, 1 for the
    refusal.
    """
    from cli.commands._managed_writer_mode import managed_writer_mode

    mode = managed_writer_mode()
    if mode is None:
        print(
            "  \u00b7 managed-writer begin: no mode decision recorded in this "
            "process; skipping the position",
            file=sys.stderr,
        )
        return 0
    if mode.state != "active":
        return 0
    from cli.commands._managed_writer_dispatch import begin_managed_writer_publication
    from shared.managed_writer_barrier import ManagedWriterBarrierError

    # The stage is entered only under the active decision: an off/blocked
    # rollout's log must not grow a managed-writer stage it never ran.
    with _stage_telemetry("managed_writer_begin"):
        try:
            begin_managed_writer_publication(target_sha)
        except ManagedWriterBarrierError as exc:
            print(
                f"\n\u2717 managed-writer begin refused: {exc}\n"
                "  nothing was stopped; the pending journal, if any, stays for "
                "`ava cluster recover-pending`",
                file=sys.stderr,
            )
            return 1
    return 0


def _collect_managed_writer_publication() -> int:
    """Adopt the completed units' post-stop facts (P2 coordinator step).

    The rollout's collection position of the managed-writer chain (task #4128,
    E2-c), reached after Phase B converged every unit and before the P5 commit
    publishes: under an `active` decision the units' post-stop facts -- the
    observed writer closure and the platform final re-read after the candidate
    is ready, gathered across the fleet -- are adopted into the durable pending
    journal. `off` / `blocked` leave the seat untouched, and a call with no
    recorded decision skips with the same visible beacon as the other steps.

    The gathering channel (the collector) is the dispatch program's to provide
    (task #4129), and that channel is not connected yet. Until it is, the step
    refuses explicitly instead of degrading: an `active` rollout must not
    proceed to publish with no collection adopted (fail-closed). Returns the
    step's exit code: 0 for a clean skip, 1 for the refusal.
    """
    from cli.commands._managed_writer_mode import managed_writer_mode

    mode = managed_writer_mode()
    if mode is None:
        print(
            "  \u00b7 managed-writer collect: no mode decision recorded in this "
            "process; skipping the position",
            file=sys.stderr,
        )
        return 0
    if mode.state != "active":
        return 0
    print(
        "\n\u2717 managed-writer collect refused: the per-unit facts channel "
        "(collector gathering, task #4129) is not connected yet, so the "
        "completed units' facts cannot be adopted and the publication cannot "
        "proceed.\n"
        "  disable the managed-writer switch or wait for the channel",
        file=sys.stderr,
    )
    return 1


def _commit_managed_writer_publication() -> int:
    """Publish the completed managed-writer activation (P5 coordinator step).

    The rollout's post-Phase-B step of the managed-writer chain: when the enable
    point resolved `active`, the units' normal-release continuations have
    recorded their readbacks in the durable pending journal, and this step
    publishes exactly that complete set through the P5 commit seat. `off` /
    `blocked` leave the seat untouched, so the pre-wiring rollout behavior is
    unchanged. A set that cannot be published refuses fail-closed: the
    journaled pending stays for checked recovery (`ava cluster
    recover-pending`), never silently dropped or cleared. Returns the step's
    exit code: 0 for a commit / clean skip / no pending entry, 1 for a refusal.

    A call with no recorded decision is an invariant breach, not a mode: the
    rollout's read point always runs before this position, so no decision can
    only mean the position was reached outside the orchestration. It skips --
    but with a visible beacon, because a silently-skipped invariant breach
    would mask the next refactor accident.
    """
    from cli.commands._managed_writer_mode import managed_writer_mode

    mode = managed_writer_mode()
    if mode is None:
        print(
            "  \u00b7 managed-writer commit: no mode decision recorded in this "
            "process; skipping the position",
            file=sys.stderr,
        )
        return 0
    if mode.state != "active":
        return 0
    from cli.commands._update_publication import commit_pending_publication
    from shared.db_transaction import write_transaction
    from shared.managed_writer_barrier import ManagedWriterBarrierError

    # The stage is entered only under the active decision: an off/blocked
    # rollout's log must not grow a managed-writer stage it never ran.
    with _stage_telemetry("managed_writer_commit"):
        try:
            with write_transaction() as conn:
                publication = commit_pending_publication(conn)
        except ManagedWriterBarrierError as exc:
            print(
                f"\n\u2717 managed-writer commit refused: {exc}\n"
                "  the pending journal remains; run `ava cluster recover-pending`",
                file=sys.stderr,
            )
            return 1
    if publication is not None:
        print(f"  \u2713 managed-writer publication committed -> {publication}")
    return 0
