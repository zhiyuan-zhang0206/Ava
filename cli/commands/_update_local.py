"""
Gateway local leg of `ava cluster update` — stop -> checkout -> sync -> start.

Split out of `cli/commands/update.py` to keep that module within the file-size
budget. This is the middle phase of the gateway orchestration, plus the
frontend-only fast path and the frontend session relaunch it shares:
- `_run_frontend_only_update` / `_restart_frontend_session` — the frontend-only
  fast path: pull + rebuild the ava-frontend session, nothing else (no
  quiesce, no migration, no backend restart, no fan-out).
- `_snapshot_known_good` — HEAD + applied-migration set, taken BEFORE anything
  is stopped (the recovery anchor for `_update_recover`).
- `_sync_grafana_provisioning` — refresh Grafana provisioning copies from the
  just-checked-out dashboard sources (non-fatal).
- `_checkout_and_sync` — force-checkout the pinned rollout commit + `uv sync`.
- `_boot_gateway_fresh` — `ava start` in a fresh subprocess so start loads the
  synced revision, not this stale interpreter (start applies pending migrations
  itself early in boot).
- `_run_gateway_local_update` — the composed local leg; every failure recovers
  to last-known-good before returning non-zero (a KeyboardInterrupt included).

Re-imported by `cli/commands/update.py` (and re-exported through `cli.commands`)
so `cli.commands(.update)._run_gateway_local_update` / `._run_frontend_only_update`
/ `._restart_frontend_session` / `._sync_grafana_provisioning` keep resolving.

"""

from __future__ import annotations

import shlex
import shutil
import sys
from pathlib import Path

from cli.commands._repo import session_name
from cli.commands._update_git import GitPullFailed, GitPullResult
from cli.commands._update_recover import _recover_rc
from cli.commands._update_report import _print_local_launch_failure_block
from shared.paths import ava_home

# `ava cluster update` restarts only the side that actually changed. A frontend-only
# pull just rebuilds the ava-frontend session — no agent quiesce, no migration,
# no backend restart, no agent-runner fan-out (a frontend change cannot touch the
# schema or backend daemons). A backend-only pull leaves the frontend serving
# (skips the ~30-60s npm build). Classification runs BEFORE the pull (git fetch
# + diff against origin/main) so the frontend-only fast path can skip Phase A.
# The frontend/backend/doc partition itself lives in `shared.repo_change`
# (`_classify_change` above is the re-export) so the gateway's read-only update
# preflight shares one source of truth.

_FRONTEND_SESSION = "frontend"


def _restart_frontend_session(repo: Path) -> bool:
    """Graceful-stop + relaunch the frontend session (rebuilds).

    Returns whether the session is up. The relaunch is retried once before that
    answer is False: `new-session` refusals are overwhelmingly transient, and the
    only alternative to a retry on this path is failing a whole rollout over a
    momentary backend hiccup.
    """
    import cli.commands as _ns
    from cli.commands._repo import _ensure_frontend_deps, build_services
    from cli.commands._session_lifecycle import _relaunch_once

    spec = next(s for s in build_services() if s.session == _FRONTEND_SESSION)
    sess = session_name(spec.session)
    print(f"\n→ restart {sess} (rebuild ~30-60s)")
    _ns._graceful_kill_session(sess, expected=True)
    _ensure_frontend_deps(repo)
    if _ns._new_session(sess, spec.cmd, repo):
        return True
    return _relaunch_once(sess, spec.cmd, repo, None)


def _run_frontend_only_update(repo: Path, origin: str) -> int:
    """Frontend-only fast path: pull + rebuild the frontend session, nothing
    else. No Phase A pause, no agent quiesce, no migration, no backend restart,
    no agent-runner fan-out.

    Returns 1 when the frontend session did not come back. The pull has already
    landed by then, so the pin still advances — the code on disk IS the new
    commit, and an un-advanced pin would have the watchdog flag this host off-pin
    once a minute forever. What changes is the verdict: this path used to return
    0 unconditionally, which is how prod's 2026-08-06 rollout printed
    `✗ failed to start ava-frontend` and `rc=0` in the same block and left the
    UI dark for three minutes with nobody told.

    This path returns before the orchestration's `finally`, so it prints its own
    aftermath block rather than reaching `finalize_rollout`.
    """
    print("\n→ frontend-only change: rebuild frontend, leave backend + agents untouched")
    print("\n→ git pull origin main")
    from cli.commands import update as _up_mod

    try:
        pull: GitPullResult = _up_mod.git_pull_main()
    except Exception as e:
        print(f"  ✗ {e}", file=sys.stderr)
        return 1
    print(f"  ✓ {pull.from_sha[:7]} → {pull.to_sha[:7]} ({pull.behind} commits)")
    frontend_up = _up_mod._restart_frontend_session(repo)
    # The pull moved HEAD, so the standing pin must move with it — otherwise the
    # gateway's watchdog flags itself off-pin once a minute until the next full
    # rollout. Safe: the new commit's backend tree is identical to the running one.
    import shared.running_sha as _rsha

    _rsha.set(pull.to_sha)
    _up_mod._persist_cluster_pin(pull.to_sha, origin=origin)
    if not frontend_up:
        _print_local_launch_failure_block([session_name(_FRONTEND_SESSION)])
        return 1
    return 0


def _snapshot_known_good(*, pull: bool, target_sha: str | None) -> tuple[str, set[str]] | None:
    """Last-known-good snapshot (HEAD + applied-migration set) for the pull path,
    taken BEFORE anything is stopped.

    The schema snapshot is from_sha's applied set — `ava start` is what applies
    this update's migrations and has not run yet, so this is the pre-update set.
    Reading it must precede any data-plane teardown: taking it after a stop is
    the 2026-07-20 incident, where a stop that was meant to keep pg/redis
    actually took them down and the snapshot's connect hit connection-refused,
    crashing the whole rollout. keep_infra=True in the caller already keeps
    pg/redis up for the migrate, but snapshotting first makes "snapshot precedes
    any teardown" a structural invariant rather than a consequence of the
    keep_infra plumbing staying correct. A failure here (DB unreachable)
    propagates: with no snapshot there is nothing to recover to, so blowing up
    loudly — while the gateway is still up — is the right outcome.

    Returns None on the restart-only path (nothing to roll back to).
    """
    if not pull:
        return None
    if target_sha is None:
        raise ValueError("_run_gateway_local_update(pull=True) requires a target_sha")
    from cli.commands import update as _up_mod

    return (_up_mod.git_head_sha(), _up_mod.current_schema_state())


def _sync_grafana_provisioning(repo: Path) -> None:
    """After checkout+sync: refresh the Grafana provisioning copies from the
    newly-checked-out dashboard sources.

    #975: the term-alignment batch updated `dashboards/ops/*.json` +
    `dashboards/ops/alerts/rules.yml` to `event_name`, but nothing refreshed the
    runtime copies under `$AVA_HOME/grafana/provisioning/` — the dashboards had
    been generated at an earlier deploy and the alert rules written even
    earlier, so after the `events.kind` column rename every panel died with a
    db query error. Grafana's file provider reloads a changed file within
    ~30s, so a rollout now carries its dashboard refresh with it.

    Only the Grafana host has the provisioning dir; agent-runners skip via the
    directory check. Non-fatal by design: a failure here must not roll a
    healthy rollout back — it only delays the dashboard refresh (warned).
    """
    provisioning = ava_home() / "grafana" / "provisioning"
    if not (provisioning / "dashboards").is_dir():
        return  # not the Grafana host
    from cli.commands import update as _up_mod

    try:
        print("\n→ sync Grafana dashboards + alert rules (provisioning as code)")
        gen = _up_mod.subprocess.run(
            [
                str(repo / ".venv" / "bin" / "python"),
                "scripts/gen_plugin_dashboard.py",
            ],
            cwd=repo,
            capture_output=False,
            check=False,
        )
        if gen.returncode != 0:
            print("  ⚠ gen_plugin_dashboard.py failed — dashboards stay stale", file=sys.stderr)
            return
        rules_src = repo / "dashboards" / "ops" / "alerts" / "rules.yml"
        rules_dst = provisioning / "alerting" / "rules.yml"
        if rules_src.is_file() and rules_dst.parent.is_dir():
            shutil.copy2(rules_src, rules_dst)
            print("  ✓ alert rules → provisioning/alerting/rules.yml")
        contact_src = repo / "dashboards" / "ops" / "alerts" / "contact.yml"
        contact_dst = provisioning / "alerting" / "contact.yml"
        if contact_src.is_file() and contact_dst.parent.is_dir():
            # The webhook contact point rides the rollout too (Task #1224):
            # contact-point provisioning hot-reloads, so the endpoint cutover
            # lands with the gateway code that serves it — never a window
            # where Grafana posts the new /api/alerts to old gateway code.
            shutil.copy2(contact_src, contact_dst)
            print("  ✓ alert contact point → provisioning/alerting/contact.yml")
        datasources_src = repo / "dashboards" / "ops" / "alerts" / "datasources.yml"
        datasources_dst = provisioning / "datasources" / "datasources.yml"
        if datasources_src.is_file() and datasources_dst.parent.is_dir():
            # The Loki + Prometheus datasources the migrated R1-R7 rules
            # query (Task #1224). Datasource provisioning hot-reloads, and
            # the rules.yml copy above lands in the same rollout — the
            # datasources must never arrive after the rules that use them.
            shutil.copy2(datasources_src, datasources_dst)
            print("  ✓ alert datasources → provisioning/datasources/datasources.yml")
    except Exception as exc:
        print(f"  ⚠ Grafana provisioning sync failed (non-fatal): {exc}", file=sys.stderr)


def _checkout_and_sync(
    repo: Path,
    target_sha: str,
    pull_recover: tuple[str, set[str]],
    preserve_frontend: frozenset[str],
) -> int | None:
    """Force-checkout the pinned rollout commit + `uv sync`; returns a recovery rc
    on failure, None on success.

    Not `git pull origin main`: the host lands on the exact rollout commit from
    any branch/dirty state, so all nodes match. The just-installed commit is
    recorded so the source-integrity guard at `ava start` can detect future
    manual git operations.
    """
    from_sha = pull_recover[0]

    # 2) force-checkout the pinned target (not `git pull origin main`): lands on
    #    the exact rollout commit from any branch/dirty state, so all nodes match.
    print(f"\n→ git checkout {target_sha[:7]} (pinned rollout target)")
    from cli.commands import update as _up_mod

    try:
        _up_mod.git_checkout_sha(target_sha)
    except GitPullFailed as e:
        print(f"  ✗ {e}", file=sys.stderr)
        return _recover_rc(repo, pull_recover, preserve_frontend)
    print(f"  ✓ {from_sha[:7]} → {target_sha[:7]}")

    # 3) uv sync (new code may introduce dependencies)
    print("\n→ uv sync")
    sync_result = _up_mod.subprocess.run(
        ["uv", "sync"],
        cwd=repo,
        capture_output=False,
        check=False,
    )
    if sync_result.returncode != 0:
        print("  ✗ uv sync failed", file=sys.stderr)
        return _recover_rc(repo, pull_recover, preserve_frontend)

    # 3.5) dashboard sources live in the just-checked-out tree — refresh the
    # runtime Grafana provisioning copies now (see _sync_grafana_provisioning;
    # #975: stale kind-era dashboards died after the events column rename).
    # Run via a subprocess of the NEW tree's venv: this updater process
    # executes the PRE-checkout module, so copy blocks added by the very
    # rollout being deployed would otherwise take effect one rollout late
    # (prod 2026-08-13: #2509's contact.yml + #2515's datasources.yml never
    # copied while rules.yml did — the old module ran the sync).
    _up_mod.subprocess.run(
        [
            str(repo / ".venv" / "bin" / "python"),
            "scripts/sync_grafana_provisioning.py",
            str(repo),
        ],
        cwd=repo,
        capture_output=False,
        check=False,
    )

    # Record the just-installed commit so the source-integrity guard at
    # `ava start` can detect future manual git operations.
    try:
        from shared.source_integrity import set_installed

        set_installed(target_sha)
    except Exception as exc:
        print(f"  · installed_sha update failed (non-fatal): {exc}", file=sys.stderr)
    return None


def _boot_gateway_fresh(repo: Path, preserve_frontend: frozenset[str]) -> int:
    """`ava start` in a fresh subprocess so start loads the synced revision
    rather than this stale interpreter.

    `ava start` applies pending migrations itself early in boot (this host is
    stopped + the central DB stays up via keep_infra above, so that apply is
    still the exclusive writer); running it in the fresh child also avoids
    mixing already-imported pre-pull modules with ones imported lazily after the
    pull (which would crash on a large version jump). ava-frontend is skipped
    (and left running) on a backend-only change (restart_frontend=False),
    forwarded as `--disable-service` to the child.

    --persist-services: preserve_frontend is a transient "leave frontend running"
    for this restart, not a durable operator disable — don't let it rewrite the
    watchdog's --disable-service marker (which would wrongly re-enable the
    operator's durably-skipped services).

    --no-readiness-gate: this leg's readiness question is answered at step 6.5 by
    `_gateway_ready.await_gateway_serving`, which asks it better — off-box,
    authenticated, through the very probe each runner's preflight uses. Letting
    the child gate too would nest two waits on overlapping questions (the "two
    clocks" mistake `shared.deploy_timing` exists to prevent) and, worse, would
    route a slow `milvus` or a headless `browser` into `_recover_rc` below,
    rolling the whole cluster back to last-known-good over a service Phase B
    does not depend on. The child still waits and still prints its crosses into
    this log.
    """
    print("\n→ ava start (gateway, fresh process, new code)")
    from cli.commands import update as _up_mod

    start_args = "start --persist-services --no-readiness-gate"
    for session in preserve_frontend:
        start_args += f" --disable-service {session}"
    ava_bin = str(repo / ".venv" / "bin" / "ava")
    return _up_mod.subprocess.run(
        [ava_bin, *shlex.split(start_args)], cwd=repo, check=False
    ).returncode


def _run_gateway_local_update(
    repo: Path,
    *,
    target_sha: str | None = None,
    restart_frontend: bool = True,
    pull: bool = True,
    force_reap_agents: bool = False,
) -> int:
    """Middle phase of `cmd_update` — gateway's local stop -> checkout -> sync -> start.

    The trailing `ava start` applies pending migrations itself (early in boot),
    so there is no separate migrate step here. Extracted so the `cmd_update`
    orchestration only has fan-out + poll sections; each step fail-fasts to
    non-zero on failure (caller uses rc to decide whether to enter Phase B).

    `target_sha` is the pinned rollout commit (required when `pull`): the host
    force-checks-out exactly it rather than pulling origin/main, so every node in
    the rollout lands on the same commit. `restart_frontend=False` (backend-only
    change) leaves the `ava-frontend` session running across the
    whole stop/start — the UI source is unchanged, so there's no point paying the
    ~30-60s rebuild.

    `pull=False` (a cluster *restart*) skips the checkout + uv sync entirely — just
    stop -> start to bounce services on the current code (apply config changes).

    **A `KeyboardInterrupt` is a failure like any other here**, not an escape: it is
    caught alongside the non-zero returns and recovers to last-known-good before
    reporting, because everything after the stop below runs with the gateway down. That
    is what makes an interrupted rollout land in the same state as a failed one — see
    the `except KeyboardInterrupt` and `ops.controllers.stalled_rollout`, which
    interrupts a stalled orchestration rather than killing it for exactly this reason.
    """
    from cli.commands import update as _up_mod

    preserve_frontend: frozenset[str] = (
        frozenset() if restart_frontend else frozenset({_FRONTEND_SESSION})
    )
    # Snapshot last-known-good BEFORE stopping anything (pull path only); a
    # failure here propagates while the gateway is still up — with no snapshot
    # there is nothing to recover to (see `_snapshot_known_good`).
    pull_recover = _snapshot_known_good(pull=pull, target_sha=target_sha)

    # 0.5) force-reap straggler agents BEFORE the service stop (quiesce-timeout
    #    backstop): mark this host's still-live agents 'restarting' and kill their
    #    processes, so no agent rides the migration on old code. The restarter is
    #    already paused (Phase A) and respawns them on new code after `ava start`.
    if force_reap_agents:
        _up_mod._force_reap_local_agents()

    # 1) graceful stop gateway daemons (old schema still in place; this ensures the
    # subsequent migrate is not hit by local daemons running old code).
    # **keep_infra=True** — the next step (apply migrations) still needs DB;
    # stopping this cluster's pg/redis would immediately give connect-refused on
    # migration (verified in prod on 2026-05-19).
    print("\n→ stop gateway daemons (graceful, keep pg/redis up for migrations)")
    if not restart_frontend:
        print("  · keeping frontend up (backend-only change, no UI rebuild)")
    _up_mod._do_stop(
        repo,
        graceful=True,
        require_confirmation=False,
        keep_infra=True,
        preserve_sessions=preserve_frontend,
    )

    # The stop above took the gateway down too, so ANY failure below leaves the
    # gateway offline — and the orchestration's compensating cluster/resume rows
    # cannot reach the agent-runners until it is back. So each failure recovers to
    # last-known-good (`pull_recover`, set only on the pull path) before returning
    # non-zero, which revives the gateway so those resumes deliver. A restart-only
    # bounce changes no code/schema, so it has nothing to roll back.
    try:
        if pull:
            # target_sha + pull_recover are set together in `_snapshot_known_good`;
            # this guard is unreachable given it, and only re-narrows both for the
            # type checker.
            if target_sha is None or pull_recover is None:
                raise ValueError("_run_gateway_local_update(pull=True) requires a target_sha")
            # 2-3) force-checkout the pinned target + uv sync; recover on failure.
            rc = _checkout_and_sync(repo, target_sha, pull_recover, preserve_frontend)
            if rc is not None:
                return rc
        else:
            print("\n→ restart-only: skip git pull / uv sync (bounce on current code)")

        # 4) gateway boots with new code in a FRESH process so start loads the
        #    synced revision rather than this stale interpreter.
        start_rc = _boot_gateway_fresh(repo, preserve_frontend)
    except KeyboardInterrupt:
        # An interrupt IS one of the "ANY failure below" the block comment above
        # covers, and it is the one that arrives without a return value to carry the
        # verdict. Left to propagate it would skip the recovery and leave the gateway
        # stopped on a half-applied transition — checkout moved, migrations not run —
        # which is strictly worse than the state a *failed* step produces, and it is
        # the difference between "this hang became a failure" and "this hang became an
        # abort". So it takes the same branch: recover to last-known-good, then report
        # it through the same rc the caller already knows how to read (recovered vs
        # DOWN), so nothing downstream has to learn a new shape.
        #
        # This is the path `ops.controllers.stalled_rollout` drives — it interrupts a
        # rollout that has stopped making progress rather than killing it, precisely so
        # this recovery runs — and it is also what an operator's Ctrl-C now gets.
        # Deliberately swallowed rather than re-raised: the caller's `rc != 0` branch is
        # what records the outcome and resumes the fleet, and re-raising would report a
        # completed rollback as an unhandled abort.
        if pull_recover is None:
            raise  # restart-only: no snapshot, nothing to roll back to
        print("\n  ✗ interrupted mid-transition; recovering to last-known-good", file=sys.stderr)
        return _recover_rc(repo, pull_recover, preserve_frontend)
    if pull_recover is not None and start_rc != 0:
        print("  ✗ ava start failed", file=sys.stderr)
        return _recover_rc(repo, pull_recover, preserve_frontend)
    return start_rc
