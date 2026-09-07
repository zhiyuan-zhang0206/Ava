"""Agent-runner leg of `ava cluster update` — the local self-update an agent-runner runs.

Split out of `cli/commands/update.py` to keep that module within the file-size
budget. This is the in-process self-update an *operator* triggers with `ava cluster update`
on an agent-runner (the rollout's Phase B uses the detached `spawn_update` shell
instead). Re-imported by `cli/commands/update.py` (and re-exported through
`cli.commands`) so `cli.commands(.update)._run_agent_runner_self_update` keeps
resolving for the dispatch in `cmd_update` and the test seams.
"""

from __future__ import annotations

import contextlib
import os
import subprocess
import sys
from collections.abc import Generator
from pathlib import Path

from cli.commands._repo import _repo_root
from cli.commands._update_git import GitPullFailed, git_checkout_sha, git_resolve_origin_main
from cli.commands._update_uv_sync import run_uv_sync_verified
from shared.exit_codes import RESTART_DECLINED_EXIT_CODE
from shared.migrations import MigrationLayoutError, validate_migrations_at_ref
from shared.platform_backend import get_backend as platform_backend
from shared.rollout_telemetry import updater_stage


def _run_agent_runner_self_update(  # noqa: PLR0915 — one existing lock/handoff lifetime for both updater modes.
    repo: Path,
    *,
    target_sha: str | None = None,
    restart_only: bool = False,
    mode: str = "smooth",
    force_reap: bool = False,
    handoff_generation: str | None = None,
    post_checkout: bool = False,
    from_sha: str | None = None,
    bootstrap_request: Path | None = None,
) -> int:
    """`ava cluster update` implementation on an agent-runner — local self-update.

    The pre-checkout image force-checks out the pinned target, syncs its venv,
    records the installed revision, then replaces itself with the post-checkout
    leg. The boundary is load-bearing: f22f5eb -> faf061d imported a new
    ``ops.updater_outcome`` against old ``shared.deploy_timing`` in
    ``sys.modules`` and failed before quiesce. The replacement image vets the
    new tree's migrations/layout (validate-before-kill: revert + abort on a
    broken layout rather than stop into a boot that cannot migrate), gracefully
    stops, and starts a fresh `ava` child. Does not run migrations itself; the
    gateway is the single schema writer.

    `target_sha` is the pinned rollout commit: a direct operator `ava cluster update`
    leaves it None and this resolves origin/main itself. Either way the checkout is
    forced (lands from any branch / dirty state — see git_checkout_sha).

    `restart_only=True` skips the checkout + uv sync — bounce services on the
    current code. (The normal agent-runner leg of a cluster restart arrives as a
    `cluster_update` work row with `restart_only` payload → `ava restart`, not
    through here; this branch covers an operator running `ava cluster update
    --restart-only` directly on an agent-runner.)

    The child `ava start` returns the posture row to idle at the end, pulling
    this host back from paused to serving, then releases its exact maintenance
    generation after readiness — the natural resume that Phase B's poll reads
    as converged. When invoked via the
    ava-updater session (spawned by spawn_update()), this function runs in a
    detached pane so a mid-flow `ava stop` does not take itself out.
    """
    prepared = None
    if bootstrap_request is not None:
        from cli.commands._update_bootstrap import prepare_bootstrap_hop

        if (
            target_sha
            or restart_only
            or force_reap
            or handoff_generation
            or post_checkout
            or from_sha
        ):
            raise ValueError("prepared bootstrap hop cannot use source-update flags")
        prepared = prepare_bootstrap_hop(bootstrap_request)

    from shared import ui_update_state, updater_handoff
    from shared.host_deploy_state import (
        clear_updater_lease,
        release_updater_lock,
        touch_updater_lease,
        try_acquire_updater_lock,
    )

    owned_generation = prepared.resume_generation if prepared is not None else handoff_generation
    if post_checkout:
        # The pre-checkout image's flock must have survived the POSIX exec:
        # acquiring the lock here would mean it did not, and running the
        # stop/start leg unlocked would invite a concurrent updater into the
        # same checkout (task #1181's race). Fail fast rather than find out
        # on the host. The Windows continuation is a child that holds no lock
        # by design (its pre-exec parent waits and owns cleanup), so the
        # guard is POSIX-only.
        if os.name != "nt" and try_acquire_updater_lock():
            raise RuntimeError(
                "post-checkout updater unexpectedly acquired the updater lock — "
                "the pre-checkout flock did not survive the exec boundary"
            )
    elif not try_acquire_updater_lock():
        # Mutual exclusion first: two concurrent updaters on one host race each
        # other's checkout/converge writes (win 2026-08-11, task #1181 — a second
        # updater tore the schtasks XML mid-write and converge failed with
        # WinError 87, leaving the host offline). The loser declines like any other
        # preflight refusal; Phase B / the watchdog self-heal read that as
        # "provably stopped" and move on. Fail-soft: try_acquire returns False only
        # for a genuine concurrent holder, never for a filesystem quirk.
        if handoff_generation is not None:
            with contextlib.suppress(Exception), ui_update_state.lifecycle_lock():
                updater_handoff.clear(handoff_generation)
        print(
            "[updater] another self-update is already running on this host — declining "
            "(concurrent updaters race the checkout/converge writes)"
        )
        return RESTART_DECLINED_EXIT_CODE

    try:
        if not post_checkout:
            from shared.cluster import session_name

            expected_session = (
                session_name("updater")
                if handoff_generation is not None
                else f"direct-updater:pid{os.getpid()}"
            )
            with ui_update_state.lifecycle_lock():
                if prepared is not None and prepared.resume_generation is not None:
                    claimed = updater_handoff.resume_bootstrap(
                        prepared.resume_generation,
                        expected_session=expected_session,
                    )
                elif prepared is not None:
                    takeover = (
                        updater_handoff.begin_bootstrap_after_dead_owner(
                            prepared.predecessor_handoff,
                            expected_session=expected_session,
                        )
                        if prepared.predecessor_handoff is not None
                        else None
                    )
                    owned_generation = takeover.generation if takeover is not None else None
                    claimed = owned_generation is not None
                else:
                    if owned_generation is None:
                        try:
                            owned_generation = updater_handoff.begin(
                                expected_session=expected_session
                            ).generation
                        except updater_handoff.UpdaterHandoffActive:
                            owned_generation = None
                    claimed = owned_generation is not None and updater_handoff.claim_running(
                        owned_generation,
                        expected_session=expected_session,
                    )
            if not claimed or owned_generation is None:
                if owned_generation is not None:
                    with contextlib.suppress(Exception):
                        updater_handoff.clear(owned_generation)
                print(
                    "[updater] spawn handoff ownership does not match this updater — "
                    "declining without touching checkout or services"
                )
                return RESTART_DECLINED_EXIT_CODE

        # R1 (Task #1021): this process IS the updater. The DB lease is a fail-soft
        # observation used by Phase B, while the running handoff's exact PID identity
        # is the local recovery proof even if the DB write or detached-session record
        # is unavailable. Keep it for the complete updater lifetime; clearing it after
        # one successful touch would reopen the same liveness gap when that lease later
        # expires. The post-checkout image touches the same lease without trying to
        # re-acquire its inherited flock or re-claim its already-running handoff.
        if prepared is None:
            with contextlib.suppress(Exception):
                touch_updater_lease()

        try:
            if prepared is not None:
                from cli.commands._update_bootstrap import execute_bootstrap_hop

                if owned_generation is None:
                    raise RuntimeError("bootstrap updater has no owned handoff generation")
                return execute_bootstrap_hop(prepared, owned_generation)
            return _run_agent_runner_self_update_inner(
                repo,
                target_sha=target_sha,
                restart_only=restart_only,
                mode=mode,
                force_reap=force_reap,
                post_checkout=post_checkout,
                from_sha=from_sha,
                handoff_generation=owned_generation,
            )
        finally:
            if prepared is None:
                with contextlib.suppress(Exception):
                    clear_updater_lease()
            if owned_generation is not None:
                with contextlib.suppress(Exception):
                    updater_handoff.clear(owned_generation)
    finally:
        # A POSIX exec image has an empty `_updater_lock_fds`; its inherited lock
        # is released by the OS at exit. On Windows the pre-exec parent still owns
        # the descriptor and reaches this same finally after its child returns.
        release_updater_lock()


def _exec_post_checkout(argv: list[str]) -> int:
    """Run the post-checkout leg without carrying old modules into the new tree.

    POSIX ``execv`` preserves the updater PID, creation time, and inherited flock,
    so the detached handoff keeps identifying one uninterrupted updater. Windows
    replaces the PID on exec, invalidating that handoff identity; it instead waits
    for a child while the pre-exec parent retains the flock and owns final cleanup.
    """
    if os.name != "nt":
        try:
            os.execv(sys.executable, argv)  # noqa: S606 — replaces this trusted updater image
        except OSError as exc:
            print(f"[updater] post-checkout exec failed: {exc}", file=sys.stderr)
            return 1

    try:
        return subprocess.run(argv, check=False).returncode
    except OSError as exc:
        print(f"[updater] post-checkout child failed to launch: {exc}", file=sys.stderr)
        return 1


def _refresh_builtin_skills(repo: Path, ava_bin: Path) -> None:
    """Refresh repo-native skill copies after the self-update's checkout (#1289).

    Runs `ava skill update` in a FRESH subprocess on the just-landed tree — the
    same freshness rule as step 5's start: this interpreter's imports are
    pre-checkout code. Never fatal: `ava skill update` exits 1 on conflicts
    (locally edited copies, the R5 contract — nothing overwritten), reported
    and ignored here; a launcher/OSError is a warning too.
    """
    print("\n→ ava skill update (repo-native skills to the landed revision)")
    try:
        rc = subprocess.run([str(ava_bin), "skill", "update"], cwd=repo, check=False).returncode
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


@contextlib.contextmanager
def _source_switch_window() -> Generator[None, None, None]:
    """Open the source-switch window around an update leg that writes the tree.

    The checkout replaces the tree file by file while the old daemons are still
    running, so a healthcheck respawn inside the window could import a
    half-written module (the win 2026-08-12/13 restart failure class).
    `respawn_service` holds back while the window is open
    (``shared/source_switch`` + ``shared.service_respawn``); the update's own
    `ava start` runs on the verified, complete tree and relaunches everything.
    The window closes on every exit path; a crashed process leaves the marker
    to expire on its TTL (``shared/source_switch`` fails open after 900s).
    """
    from shared.source_switch import clear_switching, mark_switching

    mark_switching()
    try:
        yield
    finally:
        clear_switching()


def _run_agent_runner_self_update_inner(  # noqa: PLR0915 — the self-update's checkout -> sync -> vet -> stop -> start sequence; each step is one statement
    repo: Path,
    *,
    target_sha: str | None = None,
    restart_only: bool = False,
    mode: str = "smooth",
    force_reap: bool = False,
    post_checkout: bool = False,
    from_sha: str | None = None,
    handoff_generation: str | None = None,
) -> int:
    """The body of `_run_agent_runner_self_update` (lease wrapper above).

    The ordinary leg performs only checkout, sync, and installed-SHA bookkeeping
    before handing off. Every new-tree import begins in ``post_checkout=True`` so
    a module cached before checkout cannot skew a newly imported dependency.
    """
    # Lazy `cli.commands` for `_do_stop` (and so tests can stub it); avoids a
    # top-level cycle (cli.commands imports this module via update.py's re-export).
    #
    # This imports the old command namespace before checkout for the pre-exec leg;
    # after checkout it is used only to build the exec argv. The post-checkout image
    # imports its own command namespace from the new tree before it reaches stop.
    import cli.commands as _ns

    sha: str | None = None
    if restart_only:
        print("\n→ restart-only: skip git checkout / uv sync (bounce on current code)")
    elif not post_checkout:
        # 1+2) force-checkout the pinned target + uv sync, inside the
        # source-switch window. Phase B passes target_sha; a direct operator
        # `ava cluster update` resolves origin/main itself. The checkout lands on
        # the exact commit from any branch/dirty state. The window exists because
        # the checkout replaces the tree file by file while the old daemons are
        # still running, and a healthcheck respawn in that window could import a
        # half-written module — the defect class of win's 2026-08-12/13 restart
        # failures ('.venv' is not recognized / torn imports; see
        # shared/source_switch.py). `respawn_service` holds back while the window
        # is open; the update's own `ava start` (step 5) runs on the verified,
        # complete tree and relaunches everything.
        with _source_switch_window():
            sha = target_sha if target_sha is not None else git_resolve_origin_main()
            print(f"\n→ git checkout {sha[:7]} (pinned rollout target)")
            with updater_stage("checkout"):
                try:
                    from_sha = git_checkout_sha(sha)
                except GitPullFailed as e:
                    print(f"  ✗ {e}", file=sys.stderr)
                    return 1
            print(f"  ✓ {from_sha[:7]} → {sha[:7]}")

            print("\n→ uv sync")
            with updater_stage("uv_sync"):
                sync_result = run_uv_sync_verified(repo)
            if sync_result.returncode != 0:
                print("  ✗ uv sync failed", file=sys.stderr)
                return 1

        # Record the just-installed commit so the source-integrity guard at
        # `ava start` can detect future manual git operations. (Outside the
        # window: the tree is complete by now, and the bookmark is a file under
        # $AVA_HOME, not the tree.)
        try:
            from shared.source_integrity import set_installed

            set_installed(sha)
        except Exception as exc:
            print(f"  · installed_sha update failed (non-fatal): {exc}", file=sys.stderr)

        argv = [
            sys.executable,
            "-m",
            "cli.commands._update_agent_runner",
            "--post-checkout",
            "--target-sha",
            sha,
            "--from-sha",
            from_sha,
            "--mode",
            mode,
        ]
        if force_reap:
            argv.append("--force-reap")
        if handoff_generation is not None:
            argv.extend(("--handoff-generation", handoff_generation))
        return _exec_post_checkout(argv)
    elif target_sha is None or from_sha is None:
        raise ValueError("post_checkout requires target_sha and from_sha")
    else:
        sha = target_sha

    # 2.5) validate-before-kill: the just-checked-out tree's `ava start` (step 4)
    #    fails its migrate / schema-assert on a broken migrations/ layout
    #    (duplicate / non-contiguous version) — but only after the stop below has
    #    already taken this host down. Vet the new tree now; on a broken layout
    #    revert to the prior commit and abort with this host still serving.
    if not restart_only:
        if sha is None or from_sha is None:
            raise RuntimeError(
                "non-restart self-update reached validation without checkout revisions"
            )
        try:
            validate_migrations_at_ref(sha, repo_root=repo)
        except MigrationLayoutError as e:
            print(
                f"  ✗ refusing self-update: {sha[:7]} has a broken migration layout "
                f"({e}); reverting to {from_sha[:7]}",
                file=sys.stderr,
            )
            # The revert is another source switch — same window, same reason.
            with _source_switch_window():
                git_checkout_sha(from_sha)
                resync_result = run_uv_sync_verified(repo)
            if resync_result.returncode != 0:
                print(
                    "  ! reverted source uv sync verification failed "
                    f"(rc={resync_result.returncode})",
                    file=sys.stderr,
                )
            return 1

    # 3) preflight: probe gateway + register machine BEFORE stopping services.
    #    A transient gateway outage or network blip at this point would otherwise
    #    leave the host in "services dead, can't start" after the stop below.
    #    On failure the host keeps serving — abort without stopping.
    print("\n→ preflight probes (validate-before-kill)")
    rc = _ns._preflight_probes()
    if rc != 0:
        print(
            "  ✗ refusing self-update: preflight probes failed — host still serving",
            file=sys.stderr,
        )
        # Same contract as `ava restart`: a refusal before the stop is reported as
        # RESTART_DECLINED so a caller can tell "still serving" from "may be down".
        _ns._release_self_heal_pause()
        return RESTART_DECLINED_EXIT_CODE

    # 3.5) resolve + vet the `ava` launcher BEFORE the stop, for the same reason
    #    as every other gate above: step 5 is the only thing that brings this host
    #    back up, and a missing launcher there raises FileNotFoundError out of a
    #    host whose services are already down, with nothing left to restart them.
    #    The venv bin dir is platform-named (`bin` / `Scripts`), so the hardcoded
    #    POSIX path this used to build never existed on a Windows agent-runner —
    #    exactly the host least able to be rescued by hand.
    ava_bin = platform_backend().venv_launcher("ava", root=repo)
    if not ava_bin.exists():
        print(
            f"  ✗ refusing self-update: {ava_bin} is missing — `uv sync` did not "
            f"install the launcher, and stopping now would leave this host with "
            f"nothing able to start it. Host still serving.",
            file=sys.stderr,
        )
        return 1

    # 3.6) refresh the repo-native skill copies to the just-landed tree.
    #    Converge is bootstrap-only (R5): it lands missing copies and never
    #    updates one, so without this step the materialized builtins stay at
    #    whatever version first landed them (#1289 — ava-self-evolution stuck
    #    at 8/9). Runs as a fresh subprocess on the new tree (this interpreter
    #    is pre-checkout code), and is never fatal: conflicts are reported and
    #    left alone, and no staleness may abort a self-update. Skipped on
    #    restart-only — a bounce changes no code, so there is nothing to
    #    refresh to.
    if not restart_only:
        with updater_stage("skills"):
            _refresh_builtin_skills(repo, ava_bin)

    # Verify the same pause used by standalone restart and the gateway Phase A.
    with updater_stage("quiesce"):
        if not _ns._quiesce_local_agents(mode):
            return 1
    force_reap_agents = force_reap or mode == "force"

    # 4) graceful stop. keep_infra=True: an internal self-update bounces this
    #    host's service sessions, never the shared pg/redis — on a co-located
    #    gateway,agent-runner box stopping the data plane kills the rollout
    #    orchestrator's own DB polling mid-Phase-B (it dies before releasing
    #    the cluster update lock, blocking further rollouts for the lock TTL).
    #
    #    In-process in the fresh post-checkout image: a subprocess through the
    #    `ava` launcher would let a mid-flow `ava stop` take the updater itself
    #    out. The exec boundary means this process has no old modules in
    #    `sys.modules`, while the existing lazy session-kill chain remains a second
    #    defense against importing an unrelated stale dependency before the stop.
    with updater_stage("stop"):
        stop_rc = _ns._do_stop(
            repo,
            graceful=True,
            require_confirmation=False,
            keep_infra=True,
            force_reap_agents=force_reap_agents,
            force=force_reap_agents,
        )
        if stop_rc != 0:
            return stop_rc

    # 5) start in a FRESH process so it loads the just-synced new code. Calling
    #    cmd_start() in-process would mix already-imported old modules with the
    #    new ones imported lazily after the checkout (e.g. a stale shared.paths
    #    against a fresh shared.memory_repo) and crash on a large version jump. The
    #    child `ava start` still returns the posture row to idle at the end, pulling
    #    this host back from paused to serving.
    print("\n→ ava start (fresh process, new code)")
    # --persist-services: an internal restart must not rewrite the operator's
    # durable --disable-service marker (a no-flag start would re-enable everything).
    # Strip the settings-lite opt-out before spawning `ava start`: the
    # caller (`ava cluster update`) is a lite verb and runs with
    # AVA_CONFIG_FETCH=skip, but `start` must build full Settings —
    # otherwise the child inherits the opt-out, plants the unanchored
    # DB sentinel, and fails on a pure agent-runner that has no local
    # .env DB URL (s3-2).
    start_env = os.environ.copy()
    for _key in ("AVA_CONFIG_FETCH", "AVA_CONFIG_SOURCE"):
        start_env.pop(_key, None)
    try:
        with updater_stage("start"):
            return subprocess.run(
                [str(ava_bin), "start", "--persist-services", "--updater-telemetry"],
                cwd=repo,
                env=start_env,
                check=False,
            ).returncode
    except OSError as exc:
        # Vetted as present in step 3.5, so reaching here means it vanished (or is
        # not executable) inside the stop window. Services are already down: say so
        # loudly with the path, rather than surfacing a bare traceback on a host
        # that is now silent.
        print(
            f"  ✗ {ava_bin} failed to launch ({exc}) — this host is STOPPED and "
            f"cannot restart itself; run `ava start` on it manually.",
            file=sys.stderr,
        )
        return 1


def main(argv: list[str] | None = None) -> int:
    """Module entry for the detached updater session — `python -m cli.commands._update_agent_runner`.

    The one CLI surface `ops.cluster_deploy.spawn_update`'s POSIX chain invokes,
    so the detached `ava-updater` session runs the same in-process self-update as
    an operator's `ava cluster update` on a runner instead of a hand-built shell
    ladder (R1-6, Task #1021: execution-shape convergence). Flags mirror the
    `ClusterOpPayload` the ops server forwards to `spawn_update`:

    - `--target-sha <sha>` — the rollout's pinned commit; absent resolves
      origin/main itself (a watchdog self-heal catching the host up).
    - `--restart-only` — bounce services on the current code, no checkout/sync.
    - `--mode <smooth|force|none>` — the stop policy; `none` (the rollout's
      Phase B) reuses the completed drain before stopping local services.
    - `--force-reap` — legacy explicit interruption flag; never implied by a
      drain timeout.

    The repo resolves from this module's own location (`_repo_root`), not the
    session's cwd, so the `if cd <repo>` guard in the wrapper is belt-and-braces
    rather than load-bearing.

    Returns the self-update's rc — the same verdict `[session-exit] rc=` carries
    into the updater log for `ops.updater_outcome` (a preflight refusal is
    `RESTART_DECLINED_EXIT_CODE`, telling the reader "still serving" from "may be
    down").
    """
    # The updater runs as `python -m cli.commands._update_agent_runner`, which
    # bypasses cli/main.py, so loguru never gets a sink here (shared.log's
    # module-level logger.remove() stripped the default) and every logger.error
    # inside converge — e.g. the schtasks /Create failure detail behind
    # "watchdog-probe registration failed on Windows" (#885, #1117) — was
    # silently dropped. Attach the CLI sink set (stderr + <name>.log + events
    # pipeline) so a failed self-update's last steps are visible in the updater
    # log and on the cluster admin events surface without ssh. The postgres
    # sink raises when the DB is unreachable — a real scenario on the recovery
    # path this process exists for (gateway mid-restart) — so tolerate that:
    # the stderr + file sinks attach before it, and stderr is captured into the
    # updater log by the spawn wrapper, so the details still land somewhere.
    # Silence is deliberate: the stderr + file sinks attach before the postgres
    # sink, so an init failure still leaves the stderr sink live (captured into
    # the updater log by the spawn wrapper) — nothing diagnosable is lost.
    import argparse

    import shared.log as _log

    parser = argparse.ArgumentParser(
        prog="python -m cli.commands._update_agent_runner",
        description="In-process agent-runner self-update, invoked by the detached ava-updater session.",
    )
    parser.add_argument(
        "--bootstrap-hop",
        type=Path,
        help="internal verified restricted-ops transition; never normal activation",
    )
    parser.add_argument(
        "--target-sha",
        default=None,
        help="pinned rollout commit (default: resolve the track ref itself)",
    )
    parser.add_argument(
        "--restart-only",
        action="store_true",
        help="bounce services on current code (no checkout / uv sync)",
    )
    parser.add_argument(
        "--mode",
        choices=("smooth", "force", "none"),
        default="smooth",
        help="agent-drain policy (default: smooth)",
    )
    parser.add_argument(
        "--force-reap",
        action="store_true",
        help="legacy explicit force interruption before restarting services",
    )
    parser.add_argument(
        "--handoff-generation",
        default=None,
        help="generation-scoped pause-to-lease handoff from the detached parent",
    )
    parser.add_argument(
        "--post-checkout",
        action="store_true",
        help="internal continuation after checkout/sync in a fresh interpreter",
    )
    parser.add_argument(
        "--from-sha",
        default=None,
        help="revision to restore if the checked-out tree fails migration-layout validation",
    )
    args = parser.parse_args(argv)
    if args.bootstrap_hop is not None:
        if args.mode != "smooth":
            parser.error("--bootstrap-hop cannot use a source drain policy")
        # No source resolution, logging/file sinks, data-plane mutation or
        # converge is allowed before the verified request establishes authority.
        return _run_agent_runner_self_update(
            Path(__file__).resolve().parent,
            bootstrap_request=args.bootstrap_hop,
            target_sha=args.target_sha,
            restart_only=args.restart_only,
            force_reap=args.force_reap,
            handoff_generation=args.handoff_generation,
            post_checkout=args.post_checkout,
            from_sha=args.from_sha,
        )
    with contextlib.suppress(Exception):
        _log.init_cli_process(name="updater")
    if args.post_checkout:
        if args.target_sha is None:
            parser.error("--post-checkout requires --target-sha")
        if args.from_sha is None:
            parser.error("--post-checkout requires --from-sha")
        if args.restart_only:
            parser.error("--post-checkout is incompatible with --restart-only")
    return _run_agent_runner_self_update(
        _repo_root(),
        target_sha=args.target_sha,
        restart_only=args.restart_only,
        mode=args.mode,
        force_reap=args.force_reap,
        handoff_generation=args.handoff_generation,
        post_checkout=args.post_checkout,
        from_sha=args.from_sha,
    )


if __name__ == "__main__":
    raise SystemExit(main())
