"""Cluster rollback — `ava cluster rollback`.

Rolls the cluster back to a known-good commit: quiesce agents, rollback the
schema, git-reset to the target, restart everything, and notify agents.

Two triggers:
- **Automatic** (cron health-probe after N consecutive failures): targets
  `last_known_good_sha` from `cluster_pin`.
- **Manual** (`ava cluster rollback --to <tag|sha>`): operator (human or
  agent) picks the target explicitly.

Builds on the recovery primitives from `_update_recover.py` (schema rollback
→ git reset → start sequence) and the agent quiesce from `update.py`.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from cli.commands._health_alerts import _notify_owner
from cli.commands._update_fanout import (
    _PHASE_A_TIMEOUT_S,
    ClusterOpPayload,
    _fan_out,
    _list_agent_runners,
)
from cli.commands._update_git import (
    GitPullFailed,
    _git,
    current_schema_state,
    git_head_sha,
    git_reset_hard,
    rollback_schema_to,
)
from cli.commands._update_orchestration import _phase_b_targets
from cli.commands._update_pause import _stop_the_world
from cli.commands._update_phase_b import POLL_OK, _phase_b_and_poll, _still_converging
from cli.commands._update_uv_sync import run_uv_sync
from shared.cluster_lock import (
    SETTLE_TTL_S,
    acquire_update_lock,
    read_update_lease,
    release_update_lock,
    settle_update_lock,
)
from shared.cluster_pin import (
    clear_pending_known_good,
    set_cluster_target_sha,
    set_last_known_good_sha,
)
from shared.exit_codes import SERVICES_NOT_READY_EXIT_CODE
from shared.log import logger
from shared.machine import machine_name


def _resolve_rollback_target(to_ref: str | None) -> str:
    """Resolve a rollback target to a full SHA.

    When `to_ref` is None, reads `last_known_good_sha` from `cluster_pin`.
    When `to_ref` is a tag (e.g. `v0.8.0-20260628`), resolves it via `git
    rev-list`. When it is a raw SHA prefix, resolves it to a full SHA.

    Raises:
        GitPullFailed: git resolution failed.
        ValueError: target cannot be resolved (no last_known_good_sha set,
            tag not found, SHA not in repo).
    """
    from shared.cluster_pin import get_last_known_good_sha

    if to_ref is None:
        sha = get_last_known_good_sha()
        if sha is None:
            raise ValueError(
                "no rollback target specified and `last_known_good_sha` is not set "
                "(the cluster has not yet completed a successful rollout). "
                "Pass --to <tag|sha> to specify a target explicitly."
            )
        return sha

    # Try as a full SHA first (cheap local check).
    # fail-fast-ok: resolution probing — GitPullFailed means "not found, try next form"
    with __import__("contextlib").suppress(GitPullFailed):
        _git("cat-file", "-t", to_ref)
        return _git("rev-parse", to_ref).strip()

    # Try as a tag.
    with __import__("contextlib").suppress(GitPullFailed):
        ref = f"refs/tags/{to_ref}"
        _git("cat-file", "-t", ref)
        return _git("rev-parse", ref).strip()

    # Try `origin/<branch>`.
    for prefix in ("origin/", ""):
        try:
            ref = f"{prefix}{to_ref}"
            _git("cat-file", "-t", ref)
            return _git("rev-parse", ref).strip()
        except GitPullFailed:
            continue

    raise ValueError(
        f"cannot resolve rollback target '{to_ref}': not a SHA, tag, or branch "
        f"in the local repo. Fetch first with `git fetch origin` and retry."
    )


def _migration_set_at_commit(sha: str) -> set[str]:
    """Return the migration NAME set the code at `sha` expects applied: the
    baseline sentinel plus every migration file in `migrations/` at that commit.

    Reads migrations/ from the commit's tree via `git ls-tree` (no checkout). A
    target that predates the 2026-07-19 re-baseline carries integer-named files
    (`0001_*.sql`); those are refused, because a set-tracked DB cannot be rolled
    back across the squashed baseline.

    Raises:
        GitPullFailed: git ls-tree failed.
        ValueError: the commit has a non-timestamp (pre-cutover) migration file.
    """
    from shared.migrations import _BASELINE_NAME, _migration_stem

    out = _git("ls-tree", "--name-only", sha, "migrations/")
    names: set[str] = set()
    for f in out.splitlines():
        base = f.rsplit("/", 1)[-1]
        if not base.endswith(".sql") or base.endswith(".down.sql"):
            continue
        stem = _migration_stem(base)
        if stem is None:
            raise ValueError(
                f"commit {sha[:7]} has a non-timestamp migration file ({base}); it "
                "predates the migration re-baseline and cannot be a rollback target "
                "(a set-tracked DB cannot roll back across the squashed baseline)."
            )
        names.add(stem)
    return {_BASELINE_NAME} | names


def _validate_rollout_target(target_sha: str) -> None:
    """Pre-rollback validation: verify the target commit exists locally and that
    its migration set is a subset of the DB's applied set (rollback, not forward)."""
    try:
        _git("cat-file", "-t", target_sha)
    except GitPullFailed as e:
        raise ValueError(f"target commit {target_sha[:7]} not found in local repo") from e

    head = git_head_sha()
    if target_sha == head:
        raise ValueError(
            f"target commit {target_sha[:7]} is the current HEAD -- nothing to roll back to"
        )

    target_set = _migration_set_at_commit(target_sha)
    current = current_schema_state()
    to_add = target_set - current  # target has migrations the DB lacks -> forward
    to_roll = current - target_set  # DB has migrations to reverse

    if to_add:
        raise ValueError(
            f"target {target_sha[:7]} has {len(to_add)} migration(s) the DB has not "
            f"applied ({sorted(to_add)}) -- rollback would need to go FORWARD, which "
            "is not supported (use `ava cluster update`)"
        )
    if not to_roll:
        print(
            f"  . target {target_sha[:7]} migration set == current -- schema rollback is a no-op",
            file=sys.stderr,
        )
    else:
        print(
            f"  . target {target_sha[:7]} -- will roll back {len(to_roll)} migration(s)",
            file=sys.stderr,
        )

    print(f"  . target {target_sha[:7]} validated")


def _quiesce_agents() -> None:
    """Signal every live agent to restart and wait until none are left running.

    Retained for callers that use the gateway-only quiesce primitive. Cluster
    rollback itself uses `_stop_the_world` so remote hosted agents are drained too.
    """
    from cli.commands.update import _quiesce_all_agents as _quiesce
    from cli.commands.update import _quiesce_timeout_s

    print("\n-> quiesce: signal all agents to restart (source=system:rollback)")
    _quiesce(timeout_s=_quiesce_timeout_s("smooth"))
    print("  . all agents quiesced")


def _notify_agents_of_rollback(from_sha: str, to_sha: str) -> None:
    """Insert a system inbound for each live agent, telling them the cluster was
    rolled back, and publish a per-agent Redis wake so an idling agent sees it now
    instead of at its next SELECT recheck (symmetric with signal_live_agents_restart)."""
    import json

    import shared.db
    from shared.db_transaction import write_transaction

    payload = json.dumps({"from_sha": from_sha, "to_sha": to_sha, "reason": "rollback"})
    try:
        with write_transaction() as conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO inbound_messages (agent_id, source, kind, content) "
                "SELECT id, 'system:rollback', 'chat', %s FROM agents_meta "
                "WHERE status IN ('running', 'idling') "
                "AND lease_expires_at > now() "
                "RETURNING agent_id",
                (payload,),
            )
            agent_ids = [row[0] for row in cur.fetchall()]
            if agent_ids:
                print(f"  . notified {len(agent_ids)} agent(s) of rollback")
            # Wake each notified agent now (symmetric with signal_live_agents_restart):
            # an idling agent would otherwise see the rollback note only at its next
            # SELECT recheck. Best-effort (see publish_inbound_wake); "0" == no id.
            for aid in agent_ids:
                shared.db.publish_inbound_wake(aid, "0")
    except Exception as e:
        print(f"  . could not notify agents: {e}", file=sys.stderr)


def _run_rollback(
    target_sha: str,
    *,
    repo: Path,
    from_sha: str,
    preserve_sessions: frozenset[str] = frozenset(),
) -> int:
    """Execute the rollback sequence: schema rollback -> git reset -> ava start.

    Returns 0 on success; 1 on recoverable failure (rolled back to
    pre-rollback state); 2 on unrecoverable failure (MANUAL INTERVENTION).

    The order matters critically:
    1. Schema rollback FIRST (the .down.sql files only exist in the current tree,
       which git reset is about to discard).
    2. Git reset + uv sync.
    3. `ava start` on the old code.

    On failure, attempts to recover to `from_sha` (the pre-rollback state)."""
    from shared.migrations import MigrationError, RollbackBelowFloor

    target_set = _migration_set_at_commit(target_sha)
    current = current_schema_state()
    to_roll = current - target_set

    if to_roll:
        print(f"\n-> rollback schema: reverse {len(to_roll)} migration(s) to reach target")
        try:
            rolled = rollback_schema_to(target_set)
            print(f"  . rolled back {len(rolled)} migration(s): {rolled}")
        except RollbackBelowFloor:
            print(
                "  ** MANUAL INTERVENTION: target is below the squashed baseline (no down "
                "to reverse it). Schema was NOT rolled back; code + schema left consistent "
                "on current revision. Fix-forward or choose a newer target.",
                file=sys.stderr,
            )
            return 2
        except MigrationError as exc:
            print(
                f"  ** MANUAL INTERVENTION: a down migration failed mid-rollback ({exc}); "
                "rollback aborted atomically, so the schema is unchanged at its pre-rollback "
                "applied set and code was NOT reset. Code + schema remain consistent: "
                "fix-forward or choose a different target. The gateway is DOWN; manual "
                "intervention is required.",
                file=sys.stderr,
            )
            return 2
    else:
        print("\n-> schema rollback: no-op (target migration set == current)")

    # Git reset + uv sync
    print(f"\n-> git reset --hard {target_sha[:7]}")
    git_reset_hard(target_sha)

    print("\n-> uv sync")
    sync = run_uv_sync(repo)
    if sync.returncode != 0:
        print("  . uv sync failed -- attempting recovery to pre-rollback state", file=sys.stderr)
        print(f"\n-> recover: git reset --hard {from_sha[:7]}")
        git_reset_hard(from_sha)
        run_uv_sync(repo)
        print(
            f"  ** MANUAL INTERVENTION: rollback failed at uv sync; recovered to "
            f"pre-rollback state ({from_sha[:7]}). Schema was already rolled "
            "back and was NOT re-advanced -- schema may be behind code.",
            file=sys.stderr,
        )
        return 2

    print("  . uv sync")

    # ava start
    print(f"\n-> ava start on {target_sha[:7]}")
    start_cmd = [
        str(repo / ".venv" / "bin" / "ava"),
        "start",
        "--persist-services",
    ]
    for session in preserve_sessions:
        start_cmd += ["--disable-service", session]
    start_rc = subprocess.run(start_cmd, cwd=repo, check=False).returncode
    if start_rc == SERVICES_NOT_READY_EXIT_CODE:
        # The rollback landed: schema is at the target, code is at the target, and the
        # start ran every step. One launched service has not passed its probe yet (the
        # child named it just above). That must NOT reach the branch below, which
        # re-applies forward migrations and git-resets back to the pre-rollback commit
        # -- undoing a deliberate rollback because a `browser` or a `milvus` was slow is
        # a strictly worse outcome than the degradation it would be reacting to, and it
        # would leave the operator with neither state they asked for.
        print(
            f"  ! rolled back to {target_sha[:7]}, but service(s) above are not ready "
            f"yet; keeping the rollback -- the watchdog keeps reviving them, verify "
            f"with `ava status`",
            file=sys.stderr,
        )
        return 0
    if start_rc != 0:
        print("  . ava start failed -- attempting recovery to pre-rollback state", file=sys.stderr)
        from_set = _migration_set_at_commit(from_sha)
        if from_set - current_schema_state():
            print("  -> re-applying forward migrations to pre-rollback set")
            from cli.commands._update_git import apply_pending_migrations

            try:
                apply_pending_migrations()
            except Exception as e:
                print(f"  . forward re-apply failed: {e}", file=sys.stderr)
        git_reset_hard(from_sha)
        run_uv_sync(repo)
        subprocess.run(start_cmd, cwd=repo, check=False)
        print(
            f"  ** MANUAL INTERVENTION: rollback failed at ava start; attempted "
            f"recovery to pre-rollback state ({from_sha[:7]}). Verify with `ava status`.",
            file=sys.stderr,
        )
        return 2

    print(f"  . ava start succeeded on {target_sha[:7]}")
    return 0


def _clear_pending_after_rollback() -> None:
    """Discard an LKG candidate for the commit the gateway just left."""
    try:
        clear_pending_known_good()
    except Exception as exc:
        print(
            f"  . could not clear pending known-good: {type(exc).__name__}",
            file=sys.stderr,
        )


def _fanout_runner_rollback(
    target_sha: str,
    agent_runners: list[tuple[str, str | None]],
    *,
    keep_pin: bool,
    force_reap: bool,
) -> tuple[list[str], list[tuple[str, str | None]]]:
    """Write the rollback pin, then converge remote runners without a second drain."""
    if keep_pin:
        return [], list(agent_runners)

    set_cluster_target_sha(target_sha, set_by=f"{machine_name()}:pid{os.getpid()} origin=rollback")
    print(f"  ✓ cluster pin written back -> {target_sha[:7]}")
    fanout_targets = _phase_b_targets(agent_runners)
    if not fanout_targets:
        print("  . no remote agent-runners; this host was the whole cluster")
        return [], []

    print("\n-> runner rollback verdict: fan out and poll")
    polls = _phase_b_and_poll(
        fanout_targets,
        target_sha=target_sha,
        restart_only=False,
        force_reap=force_reap,
        mode="none",
    )
    mid_transition = _still_converging(polls)
    runner_urls = dict(agent_runners)
    hosts_to_resume = [
        (name, runner_urls.get(name))
        for name, verdict in polls.items()
        if verdict.status != POLL_OK
    ]
    if mid_transition:
        names = ", ".join(sorted(mid_transition))
        print(
            f"\n✗ ROLLBACK INCOMPLETE: {len(mid_transition)} runner(s) still "
            f"converging ({names}). The pin now points at {target_sha[:7]}, so their "
            "watchdogs converge them; the deploy lease is held for a settle window.",
            file=sys.stderr,
        )
    return mid_transition, hosts_to_resume


def _record_completed_rollback(
    from_sha: str,
    target_sha: str,
    *,
    require_confirmation: bool,
    set_known_good: bool,
    mid_transition: list[str],
) -> int:
    """Notify rollback observers only after the fleet's convergence verdict is known."""
    _notify_agents_of_rollback(from_sha, target_sha)
    if set_known_good:
        set_last_known_good_sha(
            target_sha, set_by=f"{machine_name()}:pid{os.getpid()} origin=rollback"
        )
        print(f"  . last_known_good_sha advanced -> {target_sha[:7]}")
    _note_rollback_on_last_update(from_sha, target_sha)

    trigger = "manual" if require_confirmation else "auto"
    owner_text = (
        f"[cluster-rollback] cluster rolled back {from_sha[:7]} -> {target_sha[:7]} "
        f"(trigger: {trigger})"
    )
    if mid_transition:
        owner_text += (
            f"; {len(mid_transition)} runner(s) still converging "
            f"({', '.join(sorted(mid_transition))})"
        )
    try:
        _notify_owner(owner_text)
    except Exception as exc:
        print(f"  . could not notify owner: {type(exc).__name__}", file=sys.stderr)
    logger.info(
        "[cluster] rollback: {} -> {} (trigger={}, incomplete={})",
        from_sha,
        target_sha,
        trigger,
        bool(mid_transition),
    )
    print(f"\n. cluster rolled back to {target_sha[:7]}")
    return 1 if mid_transition else 0


def _finish_rollback(
    holder: str,
    hosts_to_resume: list[tuple[str, str | None]],
    mid_transition: list[str],
    deploy_capability: ClusterOpPayload,
) -> None:
    """Compensate paused runners and retain a settle hold only when needed."""
    from ops.cluster import unpause_local_cluster

    if hosts_to_resume:
        try:
            _fan_out(
                hosts_to_resume,
                "/api/cluster/resume",
                _PHASE_A_TIMEOUT_S,
                deploy_capability,
            )
        except Exception as exc:
            print(f"  . could not resume runner(s): {type(exc).__name__}", file=sys.stderr)
    try:
        unpause_local_cluster()
    except Exception as exc:
        print(f"  . could not unpause local cluster: {exc}", file=sys.stderr)
    if not mid_transition:
        release_update_lock(holder)
        return
    try:
        settle_update_lock(holder, hosts=mid_transition)
    except Exception as exc:
        print(f"  . could not hold settle lease: {type(exc).__name__}", file=sys.stderr)
    note = f"waiting for {', '.join(sorted(mid_transition))} to reach the pin"
    print(
        f"\n⚠ holding the cluster deploy lease for up to a "
        f"{SETTLE_TTL_S / 60:.0f}m settle window: {note}. No new deploy can start "
        f"until those hosts reach the pin or the window lapses; `ava cluster status` "
        f"to watch, `ava cluster recover` to break the hold.",
        file=sys.stderr,
    )


def cmd_rollback(
    *,
    to: str | None = None,
    set_known_good: bool = False,
    keep_pin: bool = False,
    require_confirmation: bool = True,
) -> int:
    """CLI entry point for `ava cluster rollback`.

    Args:
        to: target commit (tag, SHA, or branch). None = use last_known_good_sha.
        set_known_good: after rollback, advance last_known_good_sha to the
            current target_sha (the commit we just rolled back *to* becomes
            the new known-good anchor).
        keep_pin: roll back this gateway only, leaving the cluster pin and
            remote agent-runners on their current commit.
        require_confirmation: prompt for confirmation before rolling back (set False for cron-triggered rollback).

    Returns:
        0 on success, non-zero on failure.
    """
    from cli.commands._repo import _repo_root

    repo = _repo_root()

    # Resolve target
    try:
        target_sha = _resolve_rollback_target(to)
    except ValueError as e:
        print(f"* {e}", file=sys.stderr)
        return 1

    # Prompt for confirmation (skipped with -y/--yes).
    if require_confirmation:
        head = git_head_sha()
        to_display = to or "last_known_good_sha"
        print(f"Rollback cluster from {head[:7]} -> {target_sha[:7]} (target: {to_display})")
        print("This will stop all agents, rollback the schema, and restart on the old code.")
        try:
            answer = input("Proceed? [y/N] ").strip().lower()
        except EOFError:
            answer = "n"
        if answer not in ("y", "yes"):
            print("Aborted.")
            return 0

    # Validate target (pre-kill gate)
    try:
        _validate_rollout_target(target_sha)
    except ValueError as e:
        print(f"* {e}", file=sys.stderr)
        if "is the current HEAD -- nothing to roll back to" in str(e):
            return 0
        return 1

    # Acquire update lock
    from shared.cluster_lock import update_lock_holder as _lock_holder

    holder = f"{machine_name()}:pid{os.getpid()}"
    if not acquire_update_lock(holder):
        print(
            f"\n* another cluster update is in progress (held by {_lock_holder()}); "
            "aborting rollback",
            file=sys.stderr,
        )
        return 1
    lease = read_update_lease()
    if lease is None or lease.holder != holder or lease.acquired_at is None:
        release_update_lock(holder)
        print("\n* could not capture the rollback lease identity; aborting", file=sys.stderr)
        return 1
    deploy_capability: ClusterOpPayload = {
        "deploy_holder": holder,
        "deploy_acquired_at": lease.acquired_at.isoformat(),
    }

    # Snapshot pre-rollback state before the rollout primitive pauses any host.
    from_sha = git_head_sha()
    schema_snapshot = current_schema_state()
    print(
        f"\n-> pre-rollback snapshot: {from_sha[:7]} ({len(schema_snapshot)} migration(s) applied)"
    )

    agent_runners = _list_agent_runners()
    mid_transition: list[str] = []
    # Before Phase B every runner may have been paused by stop-the-world, so
    # every early return must compensate across the original target list.
    hosts_to_resume: list[tuple[str, str | None]] = list(agent_runners)
    try:
        paused_names, all_quiesced = _stop_the_world(
            agent_runners,
            mode="smooth",
            deploy_capability=deploy_capability,
        )
        if paused_names is None:
            return 1
        rc = _run_rollback(target_sha, repo=repo, from_sha=from_sha)
        if rc != 0:
            return rc
        _clear_pending_after_rollback()
        mid_transition, hosts_to_resume = _fanout_runner_rollback(
            target_sha,
            agent_runners,
            keep_pin=keep_pin,
            force_reap=not all_quiesced,
        )
        return _record_completed_rollback(
            from_sha,
            target_sha,
            require_confirmation=require_confirmation,
            set_known_good=set_known_good,
            mid_transition=mid_transition,
        )
    finally:
        _finish_rollback(holder, hosts_to_resume, mid_transition, deploy_capability)


def _note_rollback_on_last_update(from_sha: str, target_sha: str) -> None:
    """Annotate the last-update record with this rollback, or say why it could not be.

    Never raises and never changes the record's outcome — see
    `shared.last_update.note_observed_recovery` for why the observer reports what it
    did rather than what it thinks happened.
    """
    from shared.last_update import note_observed_recovery

    try:
        note_observed_recovery(f"rolled back {from_sha[:7]} -> {target_sha[:7]}")
    except Exception as exc:  # fail-fast-ok: the rollback itself already succeeded
        print(
            f"  . could not annotate the last-update record ({exc!r}); the rollback "
            f"itself succeeded",
            file=sys.stderr,
        )
