"""
Gateway local leg of `ava cluster update` — stop -> checkout -> sync -> start.

Split out of `cli/commands/update.py` to keep that module within the file-size
budget. This is the middle phase of the gateway orchestration, plus the
frontend-only fast path and the frontend session relaunch it shares:
- `_run_frontend_only_update` / `_restart_frontend_session` — the frontend-only
  fast path: pull + rebuild the ava-frontend session, nothing else (no
  quiesce, no migration, no backend restart, no fan-out).
- `_snapshot_known_good` — HEAD + applied-migration set + verified data snapshot,
  taken BEFORE anything is stopped (the recovery anchor for `_update_recover`).
- `_checkout_and_sync` — force-checkout the pinned rollout commit + `uv sync`.
  Grafana provisioning needs no copy step here: the converge phase of the
  subsequent `ava start` copies `deploy/lgtm/config/grafana/provisioning/`
  VERBATIM into `$AVA_HOME/lgtm/native/config/provisioning/` — datasource
  and webhook URLs are Grafana-native `$__env{}` references resolved from
  the rendered runtime.env (two-state on AVA_OBSERVABILITY_URL, task #1791),
  so the checkout files are always valid. A converge that changes the
  rendered grafana config kickstarts the running Grafana; unchanged renders
  are a no-op. The compose rollback asset mounts the same always-valid
  checkout tree with its static env defaults.
- `_boot_gateway_fresh` — `ava start` in a fresh subprocess so start loads the
  synced revision, not this stale interpreter (start applies pending migrations
  itself early in boot).
- `_restart_schedule_sessions` — compatibility no-op for an already running
  older updater; persistent schedule terminals remain intact.
- `_adopt_child_data_plane_credentials` — refresh the surviving orchestrator's
  credential view after that child returns, before any pin or recovery DB write.
- `_run_gateway_local_update` — the composed local leg; every failure recovers
  to last-known-good before returning non-zero (a KeyboardInterrupt included).

Re-imported by `cli/commands/update.py` (and re-exported through `cli.commands`)
so `cli.commands(.update)._run_gateway_local_update` / `._run_frontend_only_update`
/ `._restart_frontend_session` keep resolving.

"""

from __future__ import annotations

import shlex
import sys
from pathlib import Path

from cli.commands import _update_git as _git_mod
from cli.commands import _update_uv_sync
from cli.commands._data_plane_admin_secrets import resume_pending_data_plane_admin_secrets
from cli.commands._repo import session_name
from cli.commands._update_git import GitPullFailed, GitPullResult
from cli.commands._update_recover import _recover_rc
from cli.commands._update_report import _print_local_launch_failure_block
from shared.config import refresh_data_plane_settings
from shared.rollout_handoff import child_process_env
from shared.rollout_telemetry import stage as _stage_telemetry

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


def _adopt_child_data_plane_credentials() -> None:
    """Adopt credentials a fresh ``ava start`` materialized in the unit env.

    The rollout interpreter intentionally survives the checkout while the new
    tree boots in a child process. That child can perform a one-time credential
    migration and rewrite ``$AVA_HOME/.env``; child environment changes cannot
    flow back into this parent. Refresh the credential-bearing environment and
    the shared Settings singleton before the parent writes the cluster pin or
    enters recovery, both of which open new data-plane connections.

    Only the data-plane sub-model crosses this boundary. General config has no
    live reload contract and is consumed by the fresh service processes instead.
    """
    resume_pending_data_plane_admin_secrets()
    refresh_data_plane_settings()


def _refresh_builtin_skills(repo: Path) -> None:
    """Refresh repo-native skill copies after the rollout's pull (#1289).

    Runs `ava skill update` in a FRESH subprocess on the just-landed tree, for
    the same reason the boot below is a fresh process: this interpreter's
    already-imported `cli.commands.skill` is pre-pull code, and the update
    table must be the new revision's.

    Never fatal: `ava skill update` exits 1 on conflicts (locally edited
    copies — the R5 contract, nothing overwritten), which is reported and
    ignored here, and a launcher/OSError is a warning too. Skills are derived
    state; no staleness justifies failing or rolling back the rollout.
    """
    from cli.commands import update as _up_mod

    ava_bin = str(repo / ".venv" / "bin" / "ava")  # the gateway is POSIX-only
    print("\n→ ava skill update (repo-native skills to the landed revision)")
    try:
        rc = _up_mod.subprocess.run([ava_bin, "skill", "update"], cwd=repo, check=False).returncode
    except OSError as exc:
        print(f"  ! builtin skill refresh failed (non-fatal): {exc}", file=sys.stderr)
        return
    if rc == 1:
        print(
            "  ! builtin skill refresh: conflicts reported — locally edited copies "
            "were left untouched",
            file=sys.stderr,
        )
    elif rc != 0:
        print(f"  ! builtin skill refresh failed (non-fatal): rc={rc}", file=sys.stderr)


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

    with _stage_telemetry("frontend_build"):
        spec = next(s for s in build_services() if s.session == _FRONTEND_SESSION)
        sess = session_name(spec.session)
        print(f"\n→ restart {sess} (rebuild ~30-60s)")
        _ns._graceful_kill_session(sess, expected=True)
        _ensure_frontend_deps(repo)
        if _ns._new_session(sess, spec.cmd, repo):
            return True
        return _relaunch_once(sess, spec.cmd, repo, None)


def _restart_schedule_sessions() -> None:
    """Keep schedule terminals across the same pause boundary as agent terminals.

    Their currently executing Python frame cannot be upgraded in place. A full
    stop or explicit schedule restart adopts new code at its own work boundary.
    """
    print("  · persistent schedule terminals retained across update")


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


def _snapshot_known_good(
    *, pull: bool, target_sha: str | None
) -> tuple[str, set[str], Path | None] | None:
    """Last-known-good snapshot (HEAD + applied-migration set + data dump) for the
    pull path, taken BEFORE anything is stopped.

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

    return (
        _up_mod.git_head_sha(),
        _up_mod.current_schema_state(),
        _git_mod.snapshot_pre_update_data(target_sha),
    )


def _checkout_and_sync(
    repo: Path,
    target_sha: str,
    pull_recover: tuple[str, set[str], Path | None],
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

    with _stage_telemetry("checkout"):
        try:
            _up_mod.git_checkout_sha(target_sha)
        except GitPullFailed as e:
            print(f"  ✗ {e}", file=sys.stderr)
            return _recover_rc(repo, pull_recover, preserve_frontend)
    print(f"  ✓ {from_sha[:7]} → {target_sha[:7]}")

    # 3) uv sync (new code may introduce dependencies)
    print("\n→ uv sync")
    with _stage_telemetry("uv_sync"):
        sync_result = _update_uv_sync.run_uv_sync_verified(repo)
    if sync_result.returncode != 0:
        print("  ✗ uv sync failed", file=sys.stderr)
        return _recover_rc(repo, pull_recover, preserve_frontend)

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

    --persist-services keeps frontend preservation transient without rewriting
    the operator's disabled-service choices. The existing maintenance hold
    keeps agent admission closed until the orchestration completes readiness.

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
    for session in sorted(preserve_frontend):
        start_args += f" --disable-service {session}"
    ava_bin = str(repo / ".venv" / "bin" / "ava")
    child_env = child_process_env()
    # The child applies pending migrations early in its boot, so this one stage
    # covers migrate + service start + the child's own readiness wait. The
    # child's output streams into this same log, so the finer split is visible
    # in the text; the number is what the telemetry summary carries.
    with _stage_telemetry("start"):
        return _up_mod.subprocess.run(
            [ava_bin, *shlex.split(start_args)], cwd=repo, env=child_env, check=False
        ).returncode


def _recover_interrupted_update(
    repo: Path,
    pull_recover: tuple[str, set[str], Path | None] | None,
    preserve_frontend: frozenset[str],
) -> int:
    """Route an interrupt through rollback when a pull snapshot exists."""
    if pull_recover is None:
        raise KeyboardInterrupt
    print(
        "\n  ✗ interrupted mid-transition; recovering to last-known-good",
        file=sys.stderr,
    )
    return _recover_rc(repo, pull_recover, preserve_frontend)


def _pitr_restart(origin: str) -> bool:
    """Whether this cluster restart was dispatched by the PITR activation or
    rollback seam. Those must bounce PostgreSQL too, so the ALTER SYSTEM'd WAL
    config takes effect; the generic restart keeps the data plane up."""
    return origin.startswith(("pitr-activation:", "pitr-rollback:"))


def _run_gateway_local_update(
    repo: Path,
    *,
    target_sha: str | None = None,
    pull_recover: tuple[str, set[str], Path | None] | None = None,
    restart_frontend: bool = True,
    pull: bool = True,
    force_reap_agents: bool = False,
    origin: str = "",
) -> int:
    """Middle phase of `cmd_update` — gateway's local stop -> checkout -> sync -> start.

    The trailing `ava start` applies pending migrations itself (early in boot),
    so there is no separate migrate step here. Extracted so the `cmd_update`
    orchestration only has fan-out + poll sections; each step fail-fasts to
    non-zero on failure (caller uses rc to decide whether to enter Phase B).

    `target_sha` and `pull_recover` are prepared before maintenance begins when
    `pull=True`: the host force-checks-out exactly the pinned target, while the
    recovery tuple carries the prior SHA, schema set, and verified local dump.
    The local leg never creates that snapshot after the stop-the-world window
    starts. `restart_frontend=False` (backend-only change) leaves the `ava-frontend`
    session running across the
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
    if pull:
        if target_sha is None:
            raise ValueError("_run_gateway_local_update(pull=True) requires a target_sha")
        if pull_recover is None:
            raise ValueError("_run_gateway_local_update(pull=True) requires pull_recover")

    # 1) graceful stop gateway daemons (old schema still in place; this ensures the
    # subsequent migrate is not hit by local daemons running old code).
    # **keep_infra=True** — the next step (apply migrations) still needs DB;
    # stopping this cluster's pg/redis would immediately give connect-refused on
    # migration (verified in prod on 2026-05-19). The one exception is a PITR-seam
    # restart: its whole point is to restart postgres on the ALTER SYSTEM'd WAL
    # config, so it bounces the data plane too.
    print(
        "\n→ stop gateway daemons (graceful, keep pg/redis up for migrations)"
        if not _pitr_restart(origin)
        else "\n→ stop gateway daemons (graceful, bounce pg/redis too — "
        "PITR WAL config must take effect)"
    )
    if not restart_frontend:
        print("  · keeping frontend up (backend-only change, no UI rebuild)")
    with _stage_telemetry("stop"):
        stop_rc = _up_mod._do_stop(
            repo,
            graceful=True,
            require_confirmation=False,
            keep_infra=not _pitr_restart(origin),
            preserve_sessions=preserve_frontend,
            force_reap_agents=force_reap_agents,
            **({"force": True} if force_reap_agents else {}),
        )
        if stop_rc != 0:
            from shared.exit_codes import STOP_INCOMPLETE_EXIT_CODE

            return STOP_INCOMPLETE_EXIT_CODE

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
            # 3.5) refresh the repo-native skill copies in ~/.ava/skills to the
            #    just-landed tree. Converge is bootstrap-only (R5): it lands
            #    missing copies and never updates one, so without this step the
            #    materialized builtins stay at whatever version first landed
            #    them (#1289 — ava-self-evolution stuck at 8/9). Never fatal:
            #    conflicts (locally edited copies) are reported and left alone,
            #    and an unexpected failure must not roll back the cluster over
            #    derived-state sync.
            _refresh_builtin_skills(repo)
        else:
            print("\n→ restart-only: skip git pull / uv sync (bounce on current code)")

        # 4) gateway boots with new code in a FRESH process so start loads the
        #    synced revision rather than this stale interpreter.
        # Capture the fresh child's outcome, then adopt before acting on it:
        # every recovery DB dial must see any transition the child journaled.
        # Adoption itself has a controlled rollback branch so it cannot mask
        # the child failure and accidentally bypass recovery.
        start_interrupted = False
        start_failure: Exception | None = None
        start_rc: int | None = None
        try:
            start_rc = _boot_gateway_fresh(repo, preserve_frontend)
        except KeyboardInterrupt:
            start_interrupted = True
        except Exception as exc:
            start_failure = exc
        try:
            _adopt_child_data_plane_credentials()
        except Exception as exc:
            if pull_recover is None:
                raise
            print(
                f"\n  ✗ failed to adopt child data-plane credentials ({exc}); "
                "recovering to last-known-good",
                file=sys.stderr,
            )
            return _recover_rc(repo, pull_recover, preserve_frontend)
        if start_interrupted:
            return _recover_interrupted_update(repo, pull_recover, preserve_frontend)
        if start_failure is not None:
            if pull_recover is None:
                raise start_failure
            print(
                f"\n  ✗ ava start raised {start_failure!r}; recovering to last-known-good",
                file=sys.stderr,
            )
            return _recover_rc(repo, pull_recover, preserve_frontend)
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
        return _recover_interrupted_update(repo, pull_recover, preserve_frontend)
    if start_rc is None:
        raise RuntimeError("ava start completed without an outcome")
    if pull_recover is not None and start_rc != 0:
        print("  ✗ ava start failed", file=sys.stderr)
        return _recover_rc(repo, pull_recover, preserve_frontend)
    # Persistent terminals, including schedule runners, retain their work across
    # pause/update. Their explicit restart owns code adoption at a work boundary.
    return start_rc


def main(argv: list[str] | None = None) -> int:
    """Accept the previous updater entry without disrupting persistent terminals."""
    import argparse

    parser = argparse.ArgumentParser(prog="python -m cli.commands._update_local")
    parser.add_argument(
        "--bounce-schedule-sessions",
        action="store_true",
        help="legacy updater compatibility; persistent schedule sessions are retained",
    )
    args = parser.parse_args(argv)
    if args.bounce_schedule_sessions:
        _restart_schedule_sessions()
        return 0
    parser.error("--bounce-schedule-sessions is required")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
