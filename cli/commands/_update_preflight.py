"""
Pre-flight steps of the gateway `ava cluster update` orchestration.

Split out of `cli/commands/update.py` to keep that module within the file-size
budget. Everything here runs BEFORE anything is paused, so a failure aborts the
rollout with the cluster untouched:
- `_changed_paths_vs_origin` — the file list the imminent update will change
  (feeds `_classify_rollout` in `_update_orchestration`).
- `_refuse_target_sha_on_gateway` — `--target-sha` is only honoured by a pure
  agent-runner; a gateway refuses it (rc=2) rather than ignore it.
- `_resolve_rollout_target` — the single commit this rollout pins every node to.
- `_run_preflight_fetch` — Phase 0: fan out a lightweight `git fetch` to every
  agent-runner, aborting on any missing acknowledgement before a host is paused.
- `_rollout_preflight` — classify the imminent change + pin the target; returns
  an early rc for the docs-only / frontend-only fast paths.

Re-imported by `cli/commands/update.py` (and re-exported through `cli.commands`)
so `cli.commands(.update)._changed_paths_vs_origin` / `._rollout_preflight` /
`.git_pull_main` seams keep resolving for tests.

"""

from __future__ import annotations

import sys
from pathlib import Path

from cli.commands._update_fanout import _PREFLIGHT_FETCH_TIMEOUT_S, _print_fan_out_results
from cli.commands._update_git import GitPullFailed, _git
from cli.commands._update_orchestration import _classify_rollout


def _changed_paths_vs_origin() -> list[str]:
    """Fetch the track target, then the files that differ between the
    last fully installed commit and that target (origin/main in `latest` mode,
    the newest release tag in `releases` mode) — what the imminent update will
    change. The installed bookmark is the authoritative update baseline; the
    running bookmark is only its legacy fallback for installations that predate
    the bookmark.

    Raises:
        GitPullFailed: any git subcommand non-zero exit. The caller falls back
            to a full restart so a fetch hiccup never causes an under-restart.
    """
    import shared.running_sha as _rsha
    import shared.source_integrity as _integrity
    from cli.commands._update_git import track_fetch, tracking_target_ref

    track_fetch()
    baseline = _integrity.get() or _rsha.get() or "HEAD"
    out = _git("diff", "--name-only", baseline, tracking_target_ref())
    return [line for line in out.splitlines() if line.strip()]


def _refuse_target_sha_on_gateway(target_sha: str) -> int:
    """`ava cluster update --target-sha` on a gateway-capable host is refused (rc=2).

    Every gateway route ignores the flag — the detached-rollout default
    (`ava cluster update` POSTs the gateway) and `--local` (`_run_gateway_orchestration`, which
    resolves its own pin via `_resolve_rollout_target`) both take no target_sha
    at all. Accepting it there tells the operator the rollout is pinned to their
    sha while origin/main is resolved live underneath them; the flag's only
    honest answer on a gateway is to refuse and name the verb that does pin a
    commit.
    """
    print(
        f"\n✗ --target-sha {target_sha[:12]} is not accepted on a gateway-capable host.\n"
        "  The rollout orchestration resolves the target itself (origin/main, once) and\n"
        "  fans that one commit out to every agent-runner, so this flag would be ignored\n"
        "  rather than honoured — and you would believe the rollout was pinned when it\n"
        "  was not. Use instead:\n"
        "    · deploy the tracked tip          -> `ava cluster update` (no flag)\n"
        "    · move the cluster to one commit  -> `ava cluster rollback --to <sha>`\n"
        "  `--target-sha` is for a pure agent-runner's own self-update — it is how Phase B\n"
        "  threads the gateway's already-resolved pin down to each runner.",
        file=sys.stderr,
    )
    return 2


def _resolve_rollout_target(*, restart_only: bool) -> str | None:
    """The single commit this rollout pins every node to — `origin/main` resolved
    once, or None for a restart-only bounce (no checkout; current code). Resolving
    here (before Phase A) means a git failure aborts with nothing paused yet.

    Raises:
        GitPullFailed: origin/main could not be resolved.
    """
    if restart_only:
        return None
    from cli.commands import update as _up_mod

    target_sha = _up_mod.git_resolve_origin_main()
    print(f"\n→ rollout target pinned: {target_sha[:7]}")
    return target_sha


def _run_preflight_fetch(
    agent_runners: list[tuple[str, str | None]],
    *,
    restart_only: bool,
) -> bool:
    """Phase 0: pre-flight `git fetch origin` on every agent-runner.

    Fan out a lightweight, non-disruptive fetch to each agent-runner's ops
    server *before* Phase A pauses anyone. A host that cannot fetch now will
    also fail the self-update checkout in Phase B — but *after* it has already
    been paused, stranding it indefinitely (the 2026-07-25 runner
    incident). Failing early here aborts the rollout with nothing paused.

    Every selected runner must answer. Unreachable is not proof of stopped
    execution: its native agents may still have database connectivity. Failure
    aborts before anyone pauses or any schema change begins.
    """
    import cli.commands as _ns

    if not agent_runners or restart_only:
        return False

    print(f"\n→ Phase 0: pre-flight git fetch on {len(agent_runners)} agent-runner(s)")
    fetch_results = _ns._fan_out(
        agent_runners,
        "/api/cluster/fetch",
        _PREFLIGHT_FETCH_TIMEOUT_S,
    )
    ok_count = sum(1 for _, status, _ in fetch_results if status == "ok")
    print(f"  {ok_count}/{len(agent_runners)} fetched ok")
    # Phase 0 and Phase A share the same per-host diagnostic formatter.
    has_fatal = _print_fan_out_results("fetch", fetch_results)
    acknowledged = {name for name, status, _ in fetch_results if status == "ok"}
    if has_fatal or acknowledged != {name for name, _ in agent_runners}:
        print(
            "\n✗ Phase 0: not every selected agent-runner confirmed fetch; "
            "aborting before Phase A (nothing paused yet)",
            file=sys.stderr,
        )
        return True
    return False


def _rollout_preflight(
    repo: Path, *, restart_only: bool, origin: str, prepare_only: bool = False
) -> tuple[int | None, bool, str | None]:
    """Classify the imminent change + pin the rollout target, before anything pauses.

    Returns (early_rc, restart_frontend, target_sha): a non-None early_rc means
    return it now (docs-only pull / frontend-only rebuild fast paths, or an
    unresolvable / vetoed target — aborting before Phase A leaves nothing
    paused). `restart_frontend` feeds the local update; `target_sha` is the
    single pinned commit every node force-checks-out (None for a restart-only
    bounce). Ordering is load-bearing: the frontend-only fast path must skip the
    pin resolution (and Phase A) entirely.
    """
    # A dry-run must inspect a pinned target even when normal classification
    # would fast-path docs or a frontend-only update; it does not modify either.
    if prepare_only:
        restart_frontend = True
    else:
        early_rc, restart_frontend = _classify_rollout(
            repo, restart_only=restart_only, origin=origin
        )
        if early_rc is not None:
            return early_rc, restart_frontend, None

    # Pin the rollout target ONCE (resolved here, threaded to the local update +
    # every agent-runner's Phase-B self-update) so all nodes check out the *same*
    # commit instead of each re-resolving a tip that moves mid-rollout (the
    # 2026-06-01 collision). Resolve before Phase A — a failure aborts with
    # nothing paused yet.
    try:
        from cli.commands import update as _up_mod

        target_sha = _up_mod._resolve_rollout_target(restart_only=restart_only)
    except GitPullFailed as e:
        print(f"\n✗ could not resolve the rollout target ({e}); aborting", file=sys.stderr)
        return 1, restart_frontend, None

    # validate-before-kill: refuse a broken-migration-layout target before Phase A
    # (2026-06-17 outage)
    if (
        not restart_only
        and target_sha
        and (rc := _up_mod._vet_rollout_target(target_sha)) is not None
    ):
        return rc, restart_frontend, target_sha
    return None, restart_frontend, target_sha
