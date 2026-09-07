"""Gateway rollout orchestration helpers — the discrete steps
`cli/commands/update.py:_run_gateway_orchestration_inner` composes.

Split out of `update.py` (which holds the orchestration itself) to keep that file
under the size ceiling; these are the standalone steps:
- `_classify_rollout` — decide what kind of change is imminent + whether to
  fast-path (docs-only / frontend-only) out of the full pause/migrate/fan-out.
- `_persist_cluster_pin` — record the cluster's standing `cluster_target_sha`
  after the gateway reaches the rollout target, and stage backend changes for
  health-probe observation before they advance `last_known_good_sha`.
- `_resolve_fanout_targets` — the fan-out target list, reconciled against a live
  probe of every host the `stopped_at` filter would drop, and reported as
  "N of M registered agent-runner(s)".
- `_phase_b_targets` — that list minus this host, because a co-located
  gateway,agent-runner box updates itself in the local leg and must not also be
  handed a self-update by its own fan-out.

`_changed_paths_vs_origin` / `_run_frontend_only_update` are looked up through the
`cli.commands` namespace (so tests can monkeypatch them) rather than imported here.
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

from cli.commands._update_git import GitPullFailed
from shared.repo_change import classify_change as _classify_change


def _persist_cluster_pin(target_sha: str, *, origin: str, advance_known_good: bool = False) -> None:
    """Record `target_sha` as the cluster's standing pin (`cluster_target_sha`)
    once the gateway's local update reaches it.

    When `advance_known_good=True` (a backend-changing rollout), the target is
    recorded as pending-known-good. The health probe promotes it only after its
    observation window, preserving the prior release as the automatic rollback
    anchor. Frontend-only / docs-only / restart-only rollouts pass
    `advance_known_good=False` (no code change, so the known-good anchor stays
    on the last real code change).

    Agent-runners converge to `target_sha` in Phase B; `ava status` compares each
    node's HEAD against it. `origin` (who triggered the rollout) rides into the
    pin's `updated_by` alongside the executing process, so the pin row alone
    answers "who moved the cluster"."""
    from shared.cluster_pin import (
        get_last_known_good_sha,
        set_cluster_target_sha,
        set_target_with_pending_known_good,
    )
    from shared.machine import machine_name

    set_by = f"{machine_name()}:pid{os.getpid()} origin={origin}"
    if advance_known_good:
        old_lkg = get_last_known_good_sha()
        old_display = old_lkg[:7] if old_lkg else "(none)"
        set_target_with_pending_known_good(target_sha, set_by=set_by)
        print(
            f"  ✓ cluster pin updated -> {target_sha[:7]} "
            f"(last-known-good NOT advanced: {old_display} pending for the observation window, "
            f"origin={origin})"
        )
    else:
        set_cluster_target_sha(target_sha, set_by=set_by)
        print(f"  ✓ cluster pin updated -> {target_sha[:7]} (origin={origin})")


def _classify_rollout(repo: Path, *, restart_only: bool, origin: str) -> tuple[int | None, bool]:
    """Classify the imminent change for the gateway orchestration.

    Returns `(early_rc, restart_frontend)`: when `early_rc` is not None the
    orchestration should return it immediately — a fast path that skips the full
    pause/quiesce/migrate/fan-out (docs-only → pull only → 0; frontend-only →
    rebuild ava-frontend → its rc; a classify/pull failure → 1). Otherwise
    `restart_frontend` says whether the full orchestration rebuilds ava-frontend
    (only when the frontend changed too). `restart_only` skips classification and
    always bounces (rebuild frontend; no pull)."""
    import cli.commands as _ns

    if restart_only:
        # A restart always bounces; rebuild the frontend too (a config change can
        # affect the UI process).
        print("\n→ cluster restart (no pull): bounce every service on current code")
        return None, True
    from shared.running_sha import get as get_running_sha
    from shared.source_integrity import get as get_installed_sha
    from shared.source_integrity import installed_sha_needs_replay

    installed_sha = get_installed_sha()
    running_sha = get_running_sha()
    if installed_sha_needs_replay(installed_sha, running_sha, repo=repo):
        print(
            "\n→ half-deployed state: installed commit is ahead of the running commit; "
            "replaying the full rollout"
        )
        return None, True
    # Classify before touching anything. On a git error, fall back to a full
    # restart (restart_frontend=True) — never under-restart on a fetch hiccup.
    try:
        paths = _ns._changed_paths_vs_origin()
    except GitPullFailed as e:
        print(f"  ⚠ could not classify change ({e}); doing a full restart", file=sys.stderr)
        return None, True
    frontend, backend = _classify_change(paths)
    if not frontend and not backend:
        print("\n→ no code change (docs-only / already up to date) — pull only, no restart")
        print("\n→ git pull origin main")
        try:
            pull = _ns.git_pull_main()
        except Exception as e:
            print(f"  ✗ {e}", file=sys.stderr)
            return 1, True
        # The pull moved HEAD; advance the standing pin with it (same reason as
        # the frontend-only fast path: an un-advanced pin reads as off-pin).
        # Docs-only: no code changed, so do NOT advance last_known_good.
        import shared.running_sha as _rsha

        _rsha.set(pull.to_sha)
        _persist_cluster_pin(pull.to_sha, origin=origin, advance_known_good=False)
        return 0, True
    if frontend and not backend:
        return _ns._run_frontend_only_update(repo, origin), True
    return None, frontend  # backend changed; rebuild frontend only if it changed too


# One short status_probe per stop-marked host. Bounded and parallel: this runs
# before Phase A, so it delays every rollout by at most this much even when a
# decommissioned host is in the table forever.
_STOPPED_PROBE_TIMEOUT_S = 5.0


async def _probe_stopped_agent_runners(
    stopped: list[tuple[str, str | None]],
) -> dict[str, str]:
    """status_probe each stop-marked agent-runner; return name -> verdict.

    Verdicts: `'live'` (answered, and answered under its own name — the marker is
    stale), `'mismatch'` (answered under a DIFFERENT machine_name, so this row's
    dial URL points at the wrong host), `'down'` (unreachable / the op failed —
    the marker agrees with reality).
    """
    from ops import cluster_rpc as cr

    async def _one(name: str, url: str | None) -> tuple[str, str]:
        try:
            result = await cr.dispatch_to_machine(
                target_machine=name,
                kind="status_probe",
                payload={},
                timeout_s=_STOPPED_PROBE_TIMEOUT_S,
                ops_url=url,
            )
        except (cr.ClusterOpUnreachable, cr.ClusterOpFailed):
            return name, "down"
        if isinstance(result, dict) and result.get("machine_name") not in (None, name):
            return name, "mismatch"
        return name, "live"

    return dict(await asyncio.gather(*[_one(name, url) for name, url in stopped]))


def _resolve_fanout_targets(*, clear_stale_markers: bool = True) -> list[tuple[str, str | None]]:
    """The rollout's agent-runner fan-out list, reconciled against a live probe of
    every host the `stopped_at` filter would drop — and reported as N of M.

    Two sources of truth used to disagree in silence. `list_agent_runners()`
    filters on `machines.stopped_at`, while the roster's `online` column means "a
    live probe answered"; a host carrying a stale stop latch therefore read
    `online` on every dashboard while sitting outside the fan-out entirely, and
    the rollout announced a bare "3" where a reader of the roster expects 4 (the
    2026-07-28 rollout on a runner). That rollout carried no migrations so the
    excluded host was harmless — a host outside the quiesce during a
    migration-carrying rollout is a host writing the central DB on old code.

    So the marker is not trusted on its own: every excluded host is probed, and
    one that answers under its own name is **re-included** in the rollout (the
    probe is the arbiter — a genuinely stopped host cannot answer) and has its
    stale composed-row marker cleared, so the roster stops contradicting the
    fan-out. A host that answers under a different name is left out and reported
    loudly: its row's dial URL points at the wrong host, which re-including would
    turn into an update fired at a stranger.

    The count line is printed unconditionally, including the all-clear case: a
    silent count is what made the exclusion invisible.
    """
    import cli.commands as _ns
    import shared.machines

    targets = _ns._list_agent_runners()
    stopped = shared.machines.list_stopped_agent_runners()
    known = len(targets) + len(stopped)
    if not stopped:
        print(f"\n→ rollout targets: {len(targets)} of {known} registered agent-runner(s)")
        return targets

    print(
        f"\n→ reconciling {len(stopped)} agent-runner(s) marked stopped against a live probe: "
        f"{', '.join(name for name, _ in stopped)}"
    )
    verdicts = asyncio.run(_probe_stopped_agent_runners(stopped))
    reconciled = list(targets)
    for name, url in stopped:
        verdict = verdicts[name]
        if verdict != "live":
            from shared.db import connect

            with connect() as conn:
                unsafe = conn.execute(
                    "SELECT 1 FROM agents_meta WHERE machine=%s AND status<>'terminated' "
                    "AND (runtime_owner IS NOT NULL OR pid IS NOT NULL "
                    "OR lifecycle_command_id IS NOT NULL OR incarnation_resources IS NOT NULL) LIMIT 1",
                    (name,),
                ).fetchone()
            if unsafe is not None:
                raise RuntimeError(
                    f"stopped marker is not execution proof for {name}; native runtime intent "
                    "is unresolved and the runner cannot confirm drain"
                )
        if verdict == "live":
            print(
                f"  ⚠ {name}: marked stopped in `machines` but its ops server ANSWERED — "
                f"the stop marker is stale (nothing clears it but a completed `ava start`). "
                f"Including it in this rollout; a live host left outside the quiesce writes "
                f"the DB on old code across the migration.",
                file=sys.stderr,
            )
            reconciled.append((name, url))
            if clear_stale_markers:
                _clear_stale_stop_marker(name)
        elif verdict == "mismatch":
            print(
                f"  ✗ {name}: marked stopped, and the probe at its registered URL answered "
                f"under a DIFFERENT machine name — this row's dial URL points at the wrong "
                f"host. NOT included in the rollout; fix the registration "
                f"(`ava cluster status` shows it as MISMATCH).",
                file=sys.stderr,
            )
        else:
            print(f"  · {name}: stopped and unreachable with no admitted native runtime — excluded")
    reconciled.sort()
    print(
        f"\n→ rollout targets: {len(reconciled)} of {known} registered agent-runner(s)"
        f" ({known - len(reconciled)} skipped)"
    )
    return reconciled


def _clear_stale_stop_marker(name: str) -> None:
    """Clear `name`'s stale composed-row stop marker; never raises.

    Best-effort by design — the reconcile above has already put the host back in
    the rollout, which is the part that protects the migration. Failing to tidy
    the roster must not abort a rollout that is otherwise ready to run.
    """
    import shared.machines

    try:
        if shared.machines.clear_stopped_marker(name):
            print(f"    ✓ cleared {name}'s stale stop marker (the probe proves it is live)")
    except Exception as exc:
        print(f"    ⚠ could not clear {name}'s stale stop marker: {exc}", file=sys.stderr)


def _phase_b_targets(
    agent_runners: list[tuple[str, str | None]],
) -> list[tuple[str, str | None]]:
    """The rollout targets **this host must not send a self-update to**: everything in
    `agent_runners` except itself.

    A single box carries `gateway,agent-runner`, so it is a legitimate row in
    `machines.list_agent_runners()` and belongs in every other phase of its own
    rollout — Phase 0's fetch and Phase A's pause are idempotent, and including it
    keeps one code path for single-box and split alike. Phase B is the one phase where
    that stops being true, because its op is not idempotent with what the local leg
    already did: the local leg checked this host out, synced it, migrated and
    `ava start`-ed it, so the fan-out's `cluster_update` is pure redundancy — and it is
    redundancy whose *first* act is killing the ava-gateway session.

    That is the 2026-08-01 rollout (issue #1151). The order it produced was: the
    readiness gate confirms the gateway is serving -> Phase B fans out -> this host's
    own leg kills the gateway it just brought up -> the remote runners' preflight
    probes, dialing that same gateway a second later, take ECONNREFUSED and decline
    (`RESTART_DECLINED_EXIT_CODE`) -> both are reported STALLED and converge only when
    the settle lease lapses ~15 minutes later. The hole was ~9 s. Nothing downstream
    could have prevented it: the gate had already passed, correctly, on a gateway that
    was serving at the moment it was asked.

    So the fix is here rather than in a longer wait somewhere else — the redundant leg
    has no purpose to preserve, and removing it removes the hole instead of making it
    survivable. The runner-side preflight budget added alongside
    (`cli.commands._repo._probe_gateway_or_die`) is defense in depth for holes this
    rollout did not open, not a second half of this fix.

    Split deployments are unaffected: a gateway-only unit does not carry
    `agent-runner`, so it was never in this list to begin with.
    """
    from shared.machine import machine_name

    me = machine_name()
    targets = [(name, url) for name, url in agent_runners if name != me]
    if len(targets) != len(agent_runners):
        print(
            f"  · excluding {me} from Phase B: it is this rollout's orchestrator and the "
            f"local leg above already checked it out, migrated it and restarted it"
        )
    return targets
