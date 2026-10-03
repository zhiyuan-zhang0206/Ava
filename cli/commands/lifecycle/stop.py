"""Local stop and restart commands over the shared native drain boundary.

Normal paths retain checkpoints and never escalate a service or data-plane
stop on timeout; a normal stop's terminal closure SIGKILLs what outlives its
bounded grace. Explicit force uses the separate legacy resource teardown.
A stop or restart closes terminals unless `--keep-service pty-sessions` keeps
the service that holds them.
"""

from __future__ import annotations

import sys
from pathlib import Path

from base.db import Database
from base.events.live.bus import EventBus
from base.sessions.pty.paths import SERVICE_UNIT
from cli.commands._repo import _repo_root, session_name
from cli.commands.lifecycle._pause_resume import exclusive_resources
from cli.commands.lifecycle.service_stop import force_close_terminals
from cli.start_runtime import StartRuntime

# The browser service runs a headed Chrome on a persistent login profile. A
# restart / backend update preserves it by default (keep_browser=True):
# bouncing it pops a window, risks a session-restore prompt, and re-attaches CDP
# for no gain — the login state is the expensive part. Only a full teardown
# (`ava stop`, `ava cluster destroy`) takes it down.
_BROWSER_SESSION = "browser"


def _stop_data_plane(*, skip_infra: bool, runner_only: bool) -> None:
    """Stop this cluster's own Postgres+Redis (data preserved on disk).

    A runner-only host has no local data plane. `skip_infra` (keep_infra — the
    the fleet update / internal-restart path) leaves the instance running so the
    following migrate/start still has DB; without this the migrate step would hit
    connect-refused now that every cluster (including `main`) owns its instance.
    Otherwise (a full `ava stop`) the private instance is torn down."""
    if runner_only:
        print("\n→ stop pg/redis: skipped (agent-runner uses the central node)")
    elif skip_infra:
        print("\n→ stop pg/redis: kept up (keep_infra — migrate/start needs DB)")
    else:
        from cli.commands.data_plane.cluster_instance import stop_cluster_instance

        stop_cluster_instance()


def _reap_cluster_chrome() -> None:
    """Finish a browser teardown the session kill could not: kill any Chrome still
    running on THIS cluster's profile.

    Called only on an explicit force teardown with `keep_browser=False`.
    Normal stop waits for Chrome to exit without escalating signals. After a `SingletonLock` handoff Chrome is no longer a
    descendant of the `ava-browser` session, so killing that session leaves it
    running on the cluster's CDP port and the next launch's port guard refuses;
    `services/browser/orphan.py` names it by the cluster's own `--user-data-dir`
    and argues why that can never select a Chrome that is not ours.

    Silent when there is nothing to reap (the common case): the session kill above
    already reported ✓, so an extra "nothing found" line would only be noise.
    Never fails the stop — a teardown that cannot reach the browser must still
    tear the rest of the cluster down, and the pre-existing manual kill remains
    the operator's fallback.
    """
    from services.browser.orphan import reap_cluster_chrome

    try:
        pids = reap_cluster_chrome()
    except Exception as exc:
        print(f"  ⚠ could not sweep this cluster's Chrome: {exc}", file=sys.stderr)
        return
    if pids:
        print(f"  ✓ reaped Chrome left outside the session: {', '.join(map(str, pids))}")


def _compute_stop_scope(
    *,
    preserve_sessions: frozenset[str],
    keep_browser: bool,
    keep_infra: bool,
) -> tuple[list[str], bool, bool]:
    """What this stop actually tears down: (service_sessions, runner_only, skip_infra).

    The browser session joins the preserve set when keep_browser is set. A host
    without the gateway capability has no local data plane; unknown (None, role
    unresolved) conservatively takes the gateway path (stops infra). skip_infra
    (keep_infra — the fleet-update / internal-restart path) leaves the instance
    running so the following migrate/start still has DB.
    """
    # Dynamic lookup for monkeypatch-aware tests.
    import cli.commands._repo as _repo_commands
    import cli.commands.lifecycle.root_driver as _root_driver_commands

    if keep_browser:
        preserve_sessions = preserve_sessions | {_BROWSER_SESSION}

    roles = _repo_commands._roles_or_none()
    runner_only = roles is not None and "agent-runner" in roles and "gateway" not in roles
    service_sessions = _root_driver_commands._root_tree_plan(preserve_sessions)
    return service_sessions, runner_only, keep_infra or runner_only


def _print_stop_plan(
    service_sessions: list[str],
    *,
    keep_terminals: bool,
    keep_browser: bool,
    runner_only: bool,
    keep_infra: bool,
) -> None:
    """The "The following will be stopped" block shown before the confirm gate."""
    # Dynamic lookup for monkeypatch-aware tests.

    print("\nThe following will be stopped:")
    print(f"  service sessions: {', '.join(service_sessions) if service_sessions else '(none)'}")
    if not keep_terminals:
        print("  persistent terminals: closed")
    if keep_browser:
        print(f"  browser: kept up ({session_name(_BROWSER_SESSION)}, login session preserved)")
    if runner_only:
        print("  infra (pg/redis): skipped (agent-runner uses the central node)")
    elif keep_infra:
        print("  infra (pg/redis): kept up (cmd_update will migrate next and needs DB)")
    else:
        print("  infra (pg/redis): this cluster's own instance stopped (data preserved on disk)")
    print()


def _confirm_stop(*, require_confirmation: bool) -> bool:
    """The stdin "y/N" gate. Returns True when the stop may proceed."""
    if not require_confirmation:
        return True
    try:
        answer = input("confirm? [y/N] ").strip().lower()
    except EOFError:
        answer = ""
    if answer != "y":
        print("aborted")
        return False
    return True


def _force_stop(
    _repo: Path,  # retained for the common stop call contract
    *,
    require_confirmation: bool = True,
    keep_infra: bool = False,
    preserve_sessions: frozenset[str] = frozenset(),
    keep_browser: bool = True,
    announce: bool = False,
) -> int:
    """Explicit force-only resource stop; normal commands use _temporary_stop.

    Preserves agent identities/data and the selected service/infra/terminal
    scope, but may interrupt work. Never entered merely because drain timed out.
    """
    # Dynamic lookup for monkeypatch-aware tests.
    import cli.commands.lifecycle.root_driver as _root_driver_commands

    _service_sessions, runner_only, skip_infra = _compute_stop_scope(
        preserve_sessions=preserve_sessions, keep_browser=keep_browser, keep_infra=keep_infra
    )
    root_preserve = preserve_sessions | (
        frozenset({_BROWSER_SESSION}) if keep_browser else frozenset[str]()
    )
    # Keeping the pty-sessions service keeps the terminals it holds.
    keep_terminals = SERVICE_UNIT in preserve_sessions
    plan_sessions = _root_driver_commands._root_tree_plan(root_preserve)
    _print_stop_plan(
        plan_sessions,
        keep_terminals=keep_terminals,
        keep_browser=keep_browser,
        runner_only=runner_only,
        keep_infra=keep_infra,
    )
    if not _confirm_stop(require_confirmation=require_confirmation):
        return 0

    # A deliberate stop must revoke the prior start's recovery authority before
    # any daemon has a chance to observe its own shutdown or a dead peer.
    from base.deploy.lifecycle import start_serving

    start_serving.clear_serving()

    if announce:
        _announce_stopping()

    # Explicit force interrupts the host process. Agent metadata/checkpoints
    # remain untouched; the next host uses its existing owner recovery. This
    # path does not fabricate drain receipts and remains usable offline. The
    # pty-sessions service outlives this step: it closes the terminals below.
    _root_driver_commands.stop_root_service_tree(
        preserve=root_preserve | {SERVICE_UNIT}, force=True
    )

    # 1.4) a teardown that asked for the browser down finishes the job: kill any
    # Chrome still running on THIS cluster's profile. The session kill above
    # cannot reach a Chrome that left the tree on a `SingletonLock` handoff, and
    # such a Chrome holds the cluster's CDP port against the next launch
    # (services/browser/orphan.py carries the identification argument). Placed
    # after step 1 on purpose: the watchdog is dead by now, so nothing relaunches
    # Chrome onto the port we just cleared.
    if not keep_browser:
        _reap_cluster_chrome()

    # Persistent terminals are closed through the service that holds them (from its
    # ledger when it is not running), then the service itself goes.
    if not keep_terminals:
        force_close_terminals()
        _root_driver_commands.stop_root_service_tree(preserve=root_preserve, force=True)

    # 2) stop the data plane (data persists on disk).
    _stop_data_plane(skip_infra=skip_infra, runner_only=runner_only)

    return 0


@exclusive_resources
def _do_stop(
    _repo: Path,
    *,
    require_confirmation: bool = True,
    keep_infra: bool = False,
    preserve_sessions: frozenset[str] = frozenset(),
    keep_browser: bool = True,
    announce: bool = False,
    teardown_extras: bool = False,
    force: bool = False,
    timeout: float = 300,
) -> int:
    """Shared stop kernel; only explicit force escalates a service stop."""
    if force:
        return _force_stop(
            _repo,
            require_confirmation=require_confirmation,
            keep_infra=keep_infra,
            preserve_sessions=preserve_sessions,
            keep_browser=keep_browser,
            announce=announce,
        )
    from cli.commands.lifecycle._temporary_stop import stop

    return stop(
        require_confirmation=require_confirmation,
        keep_infra=keep_infra,
        preserve_sessions=preserve_sessions,
        keep_browser=keep_browser,
        announce=announce,
        teardown_extras=teardown_extras,
        timeout=timeout,
    )


def cmd_stop(
    *,
    keep_infra: bool = False,
    require_confirmation: bool = True,
    stop_browser: bool = True,
    preserve_sessions: frozenset[str] = frozenset(),
    force: bool = False,
    timeout: float = 300,
) -> int:
    """Stop this unit, including terminals and infrastructure; retain its data."""
    return _do_stop(
        _repo_root(),
        require_confirmation=require_confirmation,
        keep_infra=keep_infra,
        preserve_sessions=preserve_sessions,
        keep_browser=not stop_browser,
        announce=True,
        teardown_extras=True,
        force=force,
        timeout=timeout,
    )


def _announce_stopping() -> None:
    """Best-effort POST `/api/cluster/stopping` before local teardown.

    Stamps the stopping unit's `stopped_at` and recomputes the composed
    `machines` row so the cluster view distinguishes an intentional `ava stop`
    from a crash. `home` identifies THIS unit (a co-located peer keeps its
    caps). Best-effort by design: if the gateway is unreachable we are stopping
    anyway, so we log and proceed rather than block the teardown.
    """
    from base.cluster.machine import gateway_api_base, gateway_auth_headers, machine_name
    from base.host.net.http_dial import post as dial_post
    from base.paths import ava_home

    try:
        name = machine_name()
        home = str(ava_home())
        url = f"{gateway_api_base()}/api/cluster/stopping"
        resp = dial_post(
            url, params={"machine": name, "home": home}, timeout=5.0, headers=gateway_auth_headers()
        )
        resp.raise_for_status()
        print(f"[ava stop] announced intentional shutdown of {name!r} ({home}) to the gateway")
    except Exception as e:
        print(f"[ava stop] could not announce shutdown (proceeding anyway): {e}")


def _release_self_heal_pause() -> None:
    """After a declined restart, clear a paused posture that nothing else will.

    A restart that declines never reaches `ava start`/`ava restart`'s unpause,
    so a paused posture read here has no other owner coming to release it and
    would strand the host paused. A stop or maintenance hold in progress owns
    its own pause and is left to `ava start`; a read failure leaves the host
    paused — wrongly unpausing is the worse mistake.
    """
    from base.deploy.maintenance import admission
    from base.deploy.state.host_deploy_state import read

    held = admission.snapshot()
    if (
        held is not None
        and held.maintenance is not None
        and held.maintenance.phase in ("stopping", "stopped", "starting", "ready")
    ):
        print("  · services partially stopped; holding agents until ava start passes readiness")
        return

    # The pause lives in the posture row (R1, PR5): only `paused` is this heal's business.
    try:
        state = read(Database.from_settings())
    except Exception as exc:
        print(
            f"  · leaving this host paused (could not read host_deploy_state: {exc})",
            file=sys.stderr,
        )
        return
    if state is None or state.posture != "paused":
        return  # nothing paused this host; an operator's `ava restart` changes nothing
    from ops.cluster_pause import unpause_local_cluster

    unpause_local_cluster(Database.from_settings(), EventBus.from_settings())
    print("  · unpaused this host (nothing else owns the pause; nothing was stopped)")


def _require_restart_runtime(runtime: StartRuntime) -> None:
    """Recheck the captured runtime before disruptive work."""
    from base.paths import prod_service_checkout_error

    runtime.validate()
    if problem := prod_service_checkout_error(runtime.code_root):
        raise ValueError(problem)


def _restart_runtime() -> StartRuntime:
    """Admit the currently executing checkout before restart effects."""
    runtime = StartRuntime.development(_repo_root())
    _require_restart_runtime(runtime)
    return runtime


def _cmd_restart_body(*, mode: str = "smooth") -> int:
    """Stop then start without a stdin confirmation prompt.

    Hosted agents drain through the shared stop boundary before service stop.
    Explicit force authorizes interrupting resource shutdown.
    """
    from base.agents.exit_codes import RESTART_DECLINED_EXIT_CODE
    from base.host.proc import hosting_exec_domain, hosting_supervised_session
    from cli.commands import _repo
    from cli.commands.lifecycle import _start_readiness_preflight, start

    # Admission precedes even the restart journal: an incompatible caller must
    # leave the running generation and its maintenance state untouched.
    runtime = _restart_runtime()

    # An exec-domain restart is SIGKILLed by the execute_code call's own
    # teardown — the call's whole process group, as its turn ends (nohup/& do
    # not leave the group — 2026-09-12: macmini stranded in a local pause).
    # Refuse before any pause. The restart closes persistent terminals like a stop,
    # so no shell session can host it either: a plain login shell does.
    exec_domain = hosting_exec_domain()
    if exec_domain is not None:
        print(
            f"  ✗ refusing restart: this process runs inside an agent execute_code "
            f"exec domain ({exec_domain}) — its process group is SIGKILLed when the "
            "call's turn ends, this restart with it — the host stranded mid-restart. Run "
            "it from a shell no ava session hosts (e.g. a plain ssh/login shell).",
            file=sys.stderr,
        )
        _release_self_heal_pause()  # same decline contract as the preflight refusal below
        return RESTART_DECLINED_EXIT_CODE

    # Same refusal as `cmd_update`'s in-process legs: the stop below kills every
    # service session's tree, so a restart hosted inside one of them severs
    # itself mid stop→start and leaves the host down. The detached orchestration
    # sessions (the updater ladder's own `ava restart`) are exempt.
    hosting = hosting_supervised_session()
    if hosting is not None:
        print(
            f"  ✗ refusing restart: this process runs inside supervised session "
            f"{hosting!r}, which the stop leg kills — tree included, this restart with "
            "it, leaving the host stopped. Run it from a shell no ava session hosts "
            "(e.g. a plain ssh/login shell) — host still serving",
            file=sys.stderr,
        )
        _release_self_heal_pause()  # same decline contract as the preflight refusal below
        return RESTART_DECLINED_EXIT_CODE

    repo = runtime.code_root
    print(f"[ava restart] cwd = {repo}")
    from base.deploy.lifecycle import status_journal
    from base.deploy.progress_timeout import SERVICE_READY_TIMEOUT_S
    from ops.agent_pause import PAUSE_TIMEOUT_SECONDS

    # Only the journal's opener closes it — the same owns_journal contract
    # _temporary_stop keeps (task #2898). When an outer operation's journal is
    # still running, this restart records phases into it and leaves the finish
    # to that caller instead of clobbering its diagnosis.
    owns_journal = status_journal.begin("restart")
    print(
        f"[ava restart] budget contract: stop up to {PAUSE_TIMEOUT_SECONDS:.0f}s + "
        f"start readiness up to {SERVICE_READY_TIMEOUT_S:.0f}s + bounded preflight probes; "
        "an outer caller's timeout must exceed this total. Status journal: "
        f"{status_journal.status_path()} (phase progress and the final diagnosis "
        "stay readable there even if this process is cut off)",
        flush=True,
    )

    # Preflight: probe gateway + register machine BEFORE stopping services.
    # A transient gateway outage or network blip would otherwise leave the
    # host in "services dead, can't start" after the stop below.
    # On failure the host keeps serving — abort without stopping.
    print("\n→ preflight probes (validate-before-kill)")
    with status_journal.phase("preflight"):
        rc = _repo._preflight_probes(Database.from_settings())
    if rc != 0:
        print("  ✗ refusing restart: preflight probes failed — host still serving", file=sys.stderr)
        _release_self_heal_pause()
        if owns_journal:
            status_journal.finish(RESTART_DECLINED_EXIT_CODE, error="preflight probes failed")
        return RESTART_DECLINED_EXIT_CODE

    # Start-readiness preflight (task #3165): the read-only local checks of the
    # start leg below. Same refusal contract as the probes above — nothing was
    # stopped, and rc=3 tells any settled caller "do not start over it" — and
    # every category it refuses on would also fail this restart's own start
    # leg, so a refusal never blocks a viable bounce (the findings name the
    # repairs; a restart could not have made it past them either).
    # `check_launcher=False`: this start is in-process and never execs
    # `.venv/bin/ava`; the gate's interpreter check covers the venv seam the
    # session launches actually use.
    print("\n→ start readiness preflight (validate-before-kill, local state)")
    with status_journal.phase("preflight"):
        rc = _start_readiness_preflight.preflight_start_readiness(
            repo, check_launcher=False, runtime=runtime
        )
    if rc != 0:
        print(
            "  ✗ refusing restart: start-readiness preflight failed — host still serving; "
            "nothing was stopped. Fix the findings above, then retry.",
            file=sys.stderr,
        )
        _release_self_heal_pause()
        if owns_journal:
            status_journal.finish(
                RESTART_DECLINED_EXIT_CODE, error="start-readiness preflight failed"
            )
        return RESTART_DECLINED_EXIT_CODE

    # Every restart uses the shared hosted stop kernel; a timeout never
    # silently authorizes force.
    _require_restart_runtime(runtime)

    # Restart replaces the services, persistent terminals included (they close
    # as at `ava stop`: the pty-sessions service runs the new code afterwards),
    # and keeps the private data plane (keep_infra), the browser (keep_browser)
    # and, with teardown_extras left off, Gate, the permissions helper and native LGTM.
    with status_journal.phase("stop"):
        rc = _do_stop(
            repo,
            require_confirmation=False,
            keep_infra=True,
            keep_browser=True,
            teardown_extras=False,
            force=mode == "force",
        )
    if rc != 0:
        # The quiesce paused this host; a failed stop means no `ava start` is
        # coming to restore it. Release the pause unless a stop hold owns it
        # (same contract as the refusal paths above). The stop leg's own
        # journal phases (drain / services / ...) remain readable.
        _release_self_heal_pause()
        if owns_journal:
            status_journal.finish(rc, error="stop leg failed")
        return rc
    # Keep the captured runtime and the operator's durable selection through
    # startup; neither is recaptured from a later caller or moving selector.
    with status_journal.phase("start"):
        rc = start._cmd_start_body(persist_services=False, runtime=runtime)
    if owns_journal:
        status_journal.finish(rc, error=None if rc == 0 else f"start leg failed with rc={rc}")
    return rc


def cmd_restart(*, mode: str = "smooth") -> int:
    """Stop then start without a confirmation prompt."""
    return _cmd_restart_body(mode=mode)
