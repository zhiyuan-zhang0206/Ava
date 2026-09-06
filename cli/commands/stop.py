"""Local pause, stop and restart commands over the shared native drain boundary.

Normal paths retain checkpoints and never escalate on timeout. Explicit force
uses the separate legacy resource teardown; updates preserve persistent PTYs.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from cli.commands._orphan_reap import _reap_orphan_step
from cli.commands._pause_resume import exclusive_resources
from cli.commands._repo import _repo_root, build_services, session_name
from cli.commands._session_lifecycle import (
    _stop_sessions,  # re-export: defined with the other lifecycle helpers
)
from shared.rollout_telemetry import updater_stage

# The browser service runs a headed Chrome on a persistent login profile. An
# pause / backend update preserves it by default (keep_browser=True):
# bouncing it pops a window, risks a session-restore prompt, and re-attaches CDP
# for no gain — the login state is the expensive part. Only a full teardown
# (`ava stop`, `ava cluster destroy`) takes it down.
_BROWSER_SESSION = "browser"


def _stop_data_plane(*, skip_infra: bool, runner_only: bool) -> None:
    """Stop this cluster's own Postgres+Redis (data preserved on disk).

    A runner-only host has no local data plane. `skip_infra` (keep_infra — the
    `ava cluster update` / internal-restart path) leaves the instance running so the
    following migrate/start still has DB; without this the migrate step would hit
    connect-refused now that every cluster (including `main`) owns its instance.
    Otherwise (a full `ava stop`) the private instance is torn down."""
    if runner_only:
        print("\n→ stop pg/redis: skipped (agent-runner uses the central node)")
    elif skip_infra:
        print("\n→ stop pg/redis: kept up (keep_infra — migrate/start needs DB)")
    else:
        from cli.commands._cluster_instance import stop_cluster_instance

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
    (keep_infra — the `ava cluster update` / internal-restart path) leaves the instance
    running so the following migrate/start still has DB.
    """
    # Dynamic lookup for monkeypatch-aware tests.
    import cli.commands as _ns

    if keep_browser:
        preserve_sessions = preserve_sessions | {_BROWSER_SESSION}

    roles = _ns._roles_or_none()
    runner_only = roles is not None and "agent-runner" in roles and "gateway" not in roles
    service_sessions = [
        session_name(spec.session)
        for spec in build_services()
        if spec.session not in preserve_sessions and _ns._has_session(session_name(spec.session))
    ]
    return service_sessions, runner_only, keep_infra or runner_only


def _print_stop_plan(
    service_sessions: list[str],
    *,
    reap_agents: bool,
    keep_browser: bool,
    runner_only: bool,
    keep_infra: bool,
) -> None:
    """The "The following will be stopped" block shown before the confirm gate."""
    # Dynamic lookup for monkeypatch-aware tests.
    import cli.commands as _ns

    print("\nThe following will be stopped:")
    print(f"  service sessions: {', '.join(service_sessions) if service_sessions else '(none)'}")
    if reap_agents:
        print("  persistent terminals: closed")
    if keep_browser and _ns._has_session(session_name(_BROWSER_SESSION)):
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


def _stop_terminals_force() -> None:
    """Close this unit's persistent shells on an explicit full force stop."""
    from cli.commands._maintenance_stop import _TERMINAL_NAME
    from shared.session_backend import WinprocSessionBackend, get_shell_backend

    backend = get_shell_backend()
    names = backend.list_sessions()
    if isinstance(backend, WinprocSessionBackend):
        names = [name for name in names if _TERMINAL_NAME.match(name)]
    for name in names:
        ok, _ = backend.kill_session(name, graceful=False)
        if not ok:
            raise RuntimeError(f"force stop did not close terminal {name}")


def _force_stop(
    _repo: Path,  # used by the orphan-listener sweep (step 4); kept for call-site stability
    *,
    require_confirmation: bool = True,
    keep_infra: bool = False,
    preserve_sessions: frozenset[str] = frozenset(),
    keep_browser: bool = True,
    reap_agents: bool = False,
    announce: bool = False,
    teardown_extras: bool = False,
) -> int:
    """Explicit force-only resource stop; normal commands use _temporary_stop.

    Preserves agent identities/data and the selected service/infra/terminal
    scope, but may interrupt work. Never entered merely because drain timed out.
    """
    # `_repo` imported shared.config above, so dotenv_boot has already consumed
    # AVA_HOME_OVERRIDE. Strip it only when stop actually runs: cluster down/destroy
    # needs the exemption to enter this process, but pg_ctl and future Python children
    # must not inherit it — the "cannot become ambient" boundary (F-s4-7).
    os.environ.pop("AVA_HOME_OVERRIDE", None)

    # Dynamic lookup for monkeypatch-aware tests.
    import cli.commands as _ns

    service_sessions, runner_only, skip_infra = _compute_stop_scope(
        preserve_sessions=preserve_sessions, keep_browser=keep_browser, keep_infra=keep_infra
    )
    _print_stop_plan(
        service_sessions,
        reap_agents=reap_agents,
        keep_browser=keep_browser,
        runner_only=runner_only,
        keep_infra=keep_infra,
    )
    if not _confirm_stop(require_confirmation=require_confirmation):
        return 0

    # A deliberate stop must revoke the prior start's recovery authority before
    # any daemon has a chance to observe its own shutdown or a dead peer.
    from shared import start_serving

    start_serving.clear_serving()

    if announce:
        _announce_stopping()

    # Explicit force interrupts the host process. Agent metadata/checkpoints
    # remain untouched; the next host uses its existing owner recovery. This
    # path does not fabricate drain receipts and remains usable offline.
    _stop_sessions(service_sessions)

    # 1.4) a teardown that asked for the browser down finishes the job: kill any
    # Chrome still running on THIS cluster's profile. The session kill above
    # cannot reach a Chrome that left the tree on a `SingletonLock` handoff, and
    # such a Chrome holds the cluster's CDP port against the next launch
    # (services/browser/orphan.py carries the identification argument). Placed
    # after step 1 on purpose: the watchdog is dead by now, so nothing relaunches
    # Chrome onto the port we just cleared.
    if not keep_browser:
        _ns._reap_cluster_chrome()

    # Persistent terminals are separate from the hosted service process.
    if reap_agents:
        _stop_terminals_force()

    # 2) stop the data plane (data persists on disk).
    _stop_data_plane(skip_infra=skip_infra, runner_only=runner_only)

    # 3) full-stop extras: tear down what converge registered outside the service roster —
    # the entry-port gate and the permissions-helper LaunchAgent. After the
    # sessions are dead nothing can relaunch them (no watchdog survives), so
    # this is the last step. cmd_update / cmd_restart never come here.
    if teardown_extras:
        from cli.commands._stop_extras import (
            stop_gate_service,
            stop_lgtm_services,
            stop_permissions_helper,
        )

        stop_gate_service(force=True)
        stop_permissions_helper(force=True)
        stop_lgtm_services(force=True)

    # 4) orphan-listener sweep (Task #965): a service that escaped its session
    # (a gateway whose pane died but whose process kept the port, a pidfile
    # daemon that outlived its stop) is invisible to every leg above and holds
    # the cluster port against the next start — the new process then dies on
    # 'address already in use' while the old one keeps serving. Every port this
    # unit expects to own is scanned; only a listener positively attributable
    # to this cluster's home is an orphan of this stop and gets a verified kill.
    # Foreign listeners are identified and left alone. Runs last so it also
    # catches residuals of the data-plane and extras legs; preserved ports are
    # skipped.
    _reap_orphan_step(
        _repo,
        keep_browser=keep_browser,
        keep_infra=keep_infra,
        preserve_sessions=preserve_sessions,
        keep_gate=not teardown_extras,
    )

    return 0


@exclusive_resources
def _do_stop(
    _repo: Path,
    *,
    graceful: bool = True,
    require_confirmation: bool = True,
    keep_infra: bool = False,
    preserve_sessions: frozenset[str] = frozenset(),
    keep_browser: bool = True,
    reap_agents: bool = False,
    force_reap_agents: bool = False,
    announce: bool = False,
    teardown_extras: bool = False,
    force: bool = False,
    timeout: float = 300,
) -> int:
    """Shared pause/stop kernel; only explicit force authorizes escalation.

    Legacy graceful/force_reap_agents arguments remain accepted by an in-flight
    older updater, but a timeout-derived force_reap flag is not operator consent.
    """
    del graceful, force_reap_agents
    if force:
        return _force_stop(
            _repo,
            require_confirmation=require_confirmation,
            keep_infra=keep_infra,
            preserve_sessions=preserve_sessions,
            keep_browser=keep_browser,
            reap_agents=reap_agents,
            announce=announce,
            teardown_extras=teardown_extras,
        )
    from cli.commands._temporary_stop import stop

    return stop(
        require_confirmation=require_confirmation,
        keep_infra=keep_infra,
        preserve_sessions=preserve_sessions,
        keep_browser=keep_browser,
        keep_terminals=not reap_agents,
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
        reap_agents=True,
        announce=True,
        teardown_extras=True,
        force=force,
        timeout=timeout,
    )


def cmd_pause(
    *,
    preserve_sessions: frozenset[str] = frozenset(),
    force: bool = False,
    timeout: float = 300,
) -> int:
    """Pause for maintenance, retaining infrastructure, browser and terminals."""
    return _do_stop(
        _repo_root(),
        require_confirmation=False,
        keep_infra=True,
        preserve_sessions=preserve_sessions,
        keep_browser=True,
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
    from shared.http_dial import post as dial_post
    from shared.machine import gateway_api_base, gateway_auth_headers, machine_name
    from shared.paths import ava_home

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
    """After a declined restart, clear a paused posture that nothing else
    will — but only when no cluster update owns it.

    The updater session pauses this host *before* `ava restart`; only a completed
    `ava start`/`ava restart` or the gateway's compensating resume unpauses. A
    restart that declines does neither, so a LOCAL self-heal would strand the
    host paused until the stranded-pause recovery's own bound.

    The update lock is the discriminator: a live lock means a rollout owns this
    pause and will resume it; no lock means the pause was ours, no other owner
    coming. A read failure leaves it paused — wrongly unpausing mid-rollout is
    the worse mistake. This read is deliberately the *lossy* one — a settle hold
    naming this host is not an owner either; the backstop clears it a bound
    later (the controller's finer discrimination is unsafe in the failing
    process, #1098).
    """
    from shared import maintenance
    from shared.cluster_lock import update_lock_holder
    from shared.host_deploy_state import read

    held = maintenance.snapshot()
    if (
        held is not None
        and held.maintenance is not None
        and held.maintenance.phase in ("stopping", "stopped", "starting", "ready")
    ):
        print("  · services partially stopped; holding agents until ava start passes readiness")
        return

    # The pause lives in the posture row (R1, PR5): only `paused` is this heal's business.
    try:
        state = read()
    except Exception as exc:
        print(
            f"  · leaving this host paused (could not read host_deploy_state: {exc})",
            file=sys.stderr,
        )
        return
    if state is None or state.posture != "paused":
        return  # nothing paused this host; an operator's `ava restart` changes nothing
    try:
        holder = update_lock_holder()
    except Exception as exc:
        print(
            f"  · leaving this host paused (could not read the update lock: {exc})",
            file=sys.stderr,
        )
        return
    if holder is not None:
        print(f"  · leaving this host paused — a cluster update holds the lock ({holder})")
        return
    from ops.cluster import unpause_local_cluster

    unpause_local_cluster()
    print("  · unpaused this host (no cluster update owns the pause; nothing was stopped)")


def _cmd_restart_body(
    *, quiesce: bool = False, mode: str = "smooth", force_reap: bool = False
) -> int:
    """Stop then start without a stdin confirmation prompt.

    Hosted agents drain through the shared pause boundary before service stop.
    The legacy quiesce/force_reap flags remain accepted by an in-flight older
    official updater; explicit force authorizes interrupting resource shutdown.
    """
    del quiesce  # Every restart now drains, including an ordinary operator restart.
    # Lazy + namespace lookup so tests can monkeypatch
    # `cli.commands._do_stop` / `cli.commands._cmd_start_body`.
    import cli.commands as _ns
    from shared.exit_codes import RESTART_DECLINED_EXIT_CODE
    from shared.proc import hosting_supervised_session

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

    repo = _repo_root()
    print(f"[ava restart] cwd = {repo}")

    # Each step below is timed as an `[updater] stage=` line (Task #1820): the
    # Windows updater ladder runs this command behind a cmd.exe chain whose
    # own fetch/checkout/uv markers are emitted by `ops.cluster_deploy`, and
    # `ops.updater_outcome` pairs the two into the per-host stage breakdown the
    # rollout report shows.
    # Preflight: probe gateway + register machine BEFORE stopping services.
    # A transient gateway outage or network blip would otherwise leave the
    # host in "services dead, can't start" after the stop below.
    # On failure the host keeps serving — abort without stopping.
    print("\n→ preflight probes (validate-before-kill)")
    with updater_stage("preflight"):
        rc = _ns._preflight_probes()
    if rc != 0:
        print("  ✗ refusing restart: preflight probes failed — host still serving", file=sys.stderr)
        _release_self_heal_pause()
        return RESTART_DECLINED_EXIT_CODE

    # Every restart uses the shared hosted pause kernel; a timeout never
    # silently authorizes force.

    # keep_infra=True: an internal restart bounces this host's service sessions,
    # never this cluster's own pg/redis instance — stopping the data plane mid-orchestration
    # kills the gateway orchestrator's own DB polling (same failure mode as
    # the self-update leg; see _run_agent_runner_self_update).
    with updater_stage("stop"):
        rc = _ns._do_stop(
            repo,
            graceful=True,
            require_confirmation=False,
            keep_infra=True,
            force=mode == "force" or force_reap,
        )
    if rc != 0:
        # The quiesce paused this host; a failed stop means no `ava start` is
        # coming to restore it. Release the pause unless a cluster update owns
        # it (same contract as the refusal paths above).
        _release_self_heal_pause()
        return rc
    # Internal restart: preserve the operator's durable --disable-service marker
    # (a no-flag operator start would rewrite it to empty and re-enable everything).
    with updater_stage("start"):
        return _ns._cmd_start_body(persist_services=False, updater_telemetry=True)


def cmd_restart(*, quiesce: bool = False, mode: str = "smooth", force_reap: bool = False) -> int:
    """Stop then start without a confirmation prompt, timing the full restart."""
    with updater_stage("restart"):
        return _cmd_restart_body(quiesce=quiesce, mode=mode, force_reap=force_reap)
