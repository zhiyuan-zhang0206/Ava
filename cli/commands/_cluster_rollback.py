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

from cli.commands._update_git import (
    GitPullFailed,
    _git,
    current_schema_state,
    git_head_sha,
    git_reset_hard,
    rollback_schema_to,
)
from shared.cluster_lock import acquire_update_lock, release_update_lock
from shared.exit_codes import SERVICES_NOT_READY_EXIT_CODE


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

    Reuses `_quiesce_all_agents` from `update.py`."""
    from cli.commands.update import _quiesce_all_agents as _quiesce
    from cli.commands.update import _quiesce_timeout_s

    print("\n-> quiesce: signal all agents to restart (source=system:rollback)")
    # Explicit smooth-mode timeout — the legacy 60 s default was removed with
    # _QUIESCE_TIMEOUT_S (audit #01-update-sequence #13); a rollback is a
    # graceful drain, so it waits out a healthy agent's longest execute_code.
    _quiesce(timeout_s=_quiesce_timeout_s("smooth"))
    print("  . all agents quiesced")


def _notify_agents_of_rollback(from_sha: str, to_sha: str) -> None:
    """Insert a system inbound for each live agent, telling them the cluster was
    rolled back, and publish a per-agent Redis wake so an idling agent sees it now
    instead of at its next SELECT recheck (symmetric with signal_live_agents_restart)."""
    import json

    import shared.db

    payload = json.dumps({"from_sha": from_sha, "to_sha": to_sha, "reason": "rollback"})
    try:
        with shared.db.connect(autocommit=True) as conn, conn.cursor() as cur:
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
                "earlier downs already committed, schema is PARTIALLY rolled back. "
                "Do NOT reset code. Manual fix required.",
                file=sys.stderr,
            )
            return 2
    else:
        print("\n-> schema rollback: no-op (target migration set == current)")

    # Git reset + uv sync
    print(f"\n-> git reset --hard {target_sha[:7]}")
    git_reset_hard(target_sha)

    print("\n-> uv sync")
    sync = subprocess.run(["uv", "sync"], cwd=repo, capture_output=False, check=False)
    if sync.returncode != 0:
        print("  . uv sync failed -- attempting recovery to pre-rollback state", file=sys.stderr)
        print(f"\n-> recover: git reset --hard {from_sha[:7]}")
        git_reset_hard(from_sha)
        subprocess.run(["uv", "sync"], cwd=repo, check=False)
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
        subprocess.run(["uv", "sync"], cwd=repo, check=False)
        subprocess.run(start_cmd, cwd=repo, check=False)
        print(
            f"  ** MANUAL INTERVENTION: rollback failed at ava start; attempted "
            f"recovery to pre-rollback state ({from_sha[:7]}). Verify with `ava status`.",
            file=sys.stderr,
        )
        return 2

    print(f"  . ava start succeeded on {target_sha[:7]}")
    return 0


def cmd_rollback(
    *,
    to: str | None = None,
    set_known_good: bool = False,
    require_confirmation: bool = True,
) -> int:
    """CLI entry point for `ava cluster rollback`.

    Args:
        to: target commit (tag, SHA, or branch). None = use last_known_good_sha.
        set_known_good: after rollback, advance last_known_good_sha to the
            current target_sha (the commit we just rolled back *to* becomes
            the new known-good anchor).
        require_confirmation: prompt for confirmation before rolling back (set False for cron-triggered rollback).

    Returns:
        0 on success, non-zero on failure.
    """
    from cli.commands._repo import _repo_root
    from ops.cluster import pause_local_cluster, unpause_local_cluster
    from shared.cluster_pin import set_last_known_good_sha
    from shared.machine import machine_name

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

    # Snapshot pre-rollback state
    from_sha = git_head_sha()
    schema_snapshot = current_schema_state()
    print(
        f"\n-> pre-rollback snapshot: {from_sha[:7]} ({len(schema_snapshot)} migration(s) applied)"
    )

    try:
        # Stop-the-world first — the same first step the rollout's
        # `_stop_the_world` takes: pause the LOCAL restarter (posture=paused +
        # kill the restarter session) before quiescing, so no agent exiting
        # from here on can be respawned while the schema/code transition runs.
        # Rollback used to quiesce without pausing: the restarter does not read
        # the paused posture — it only gates on gateway health, which stays up
        # through the whole rollback — so it respawned every exiting agent
        # within its 1s poll, on the PRE-reset code, and the quiesce
        # convergence poll could never converge (2026-08-08 audit finding).
        pause_local_cluster()

        # Quiesce agents
        _quiesce_agents()

        # Execute rollback
        rc = _run_rollback(
            target_sha,
            repo=repo,
            from_sha=from_sha,
        )
        if rc != 0:
            return rc

        # Notify agents
        _notify_agents_of_rollback(from_sha, target_sha)

        # Optionally advance last_known_good_sha
        if set_known_good:
            set_last_known_good_sha(
                target_sha, set_by=f"{machine_name()}:pid{os.getpid()} origin=rollback"
            )
            print(f"  . last_known_good_sha advanced -> {target_sha[:7]}")

        # Tell the status surfaces what was done about the failed update. The
        # rollback is an EXTERNAL observer of that failure — the orchestration that
        # died could file no report, but the process cleaning up after it provably
        # witnessed the death. Without this the operator sees the pin change and no
        # statement of why, which is exactly the 2026-07-30 puzzle. Best-effort and
        # last: a rollback that worked must not report failure because a note could
        # not be written.
        _note_rollback_on_last_update(from_sha, target_sha)

        print(f"\n. cluster rolled back to {target_sha[:7]}")
        return 0
    finally:
        # Bring the local host back on every exit path: a successful rollback's
        # trailing `ava start` already re-created the restarter, and a failed
        # one has had its recovery attempted — either way agents must be
        # respawnable again and the 503 posture must clear. Never masks the
        # rollback's own outcome.
        try:
            unpause_local_cluster()
        except Exception as exc:
            print(f"  . could not unpause local cluster: {exc}", file=sys.stderr)
        release_update_lock(holder)


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
