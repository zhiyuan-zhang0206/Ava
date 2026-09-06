"""`ava stop` (force-kill, confirmation) + `ava restart` (force-kill, no prompt).

Shared `_do_stop` is also called by `cli.commands.update` for the graceful
upgrade path (keep_infra=True so the in-flight migration step still has DB).
"""

from __future__ import annotations

import os
import re
import sys
import time
from pathlib import Path
from typing import Any

from cli.commands._orphan_reap import _reap_orphan_step
from cli.commands._repo import _repo_root, build_services, session_name
from cli.commands._session_lifecycle import (
    _stop_sessions,  # re-export: defined with the other lifecycle helpers
)
from shared import stop_timing
from shared.platform import IS_WINDOWS
from shared.rollout_telemetry import updater_stage

# The browser service runs a headed Chrome on a persistent login profile. An
# in-place stop / backend update preserves it by default (keep_browser=True):
# bouncing it pops a window, risks a session-restore prompt, and re-attaches CDP
# for no gain — the login state is the expensive part. Only a full teardown
# (`ava stop --stop-browser`, `ava cluster destroy`) takes it down.
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


def _reap_agent_sessions(
    *,
    timeout_s: float = stop_timing.REAP_KILL_WINDOW_S,
    kill_shells: bool = True,
) -> list[tuple[str, str]]:
    """Graceful-then-force teardown of THIS cluster's agent processes; with
    kill_shells (default), also their persistent shells.

    Agent processes are gateway-spawned (not ServiceSpecs), so `ava stop`
    reaps them here — self-contained (DB may be down; annotation fail-soft):

    1. The agent PROCESSES (`ava-agent-<id>`): detached native sessions the
       native supervisor tracks in `$AVA_HOME/run/sessions/`. SIGTERM each
       (agent runs its finally), wait one shared deadline, SIGKILL stragglers.
    2. kill_shells=True (default): force-kill every session on the shell
       backend — agent shells/watchers AND gateway-owned schedule sessions —
       `ava stop`'s full-stop semantics (the backend kills each session's
       process tree; detached per-session hosts are reachable only by name,
       so an unnamed one would outlive the cluster). False (update/restart
       force-reap): all of them persist.

    Returns [(session, mode)] (mode in {graceful, forced, child}) for the stop output.
    """
    from shared.session_backend import get_shell_backend, native_proc

    native = native_proc()  # agent processes (native)
    backend = get_shell_backend()  # agent shells / watchers (per-session pty hosts on POSIX)
    prefix = session_name("agent-")  # ava-agent-
    proc_re = re.compile(rf"^{re.escape(prefix)}\d+$")
    # Transitional (path-only cutover): agent processes started by pre-cutover
    # code are recorded as `ava-<cluster>-agent-<id>`. The native session
    # registry lives under THIS home ($AVA_HOME/run/sessions/), so any such
    # record belongs to this cluster by construction — reap it too, or old
    # agents outlive `ava stop` on a stale env. No service session matches
    # (`agent-runner-watchdog` has no trailing digits; `agent-<id>` requires
    # the digit tail).
    legacy_proc_re = re.compile(r"^ava-.+-agent-\d+$")
    proc_sessions = _agent_proc_sessions(native, proc_re, legacy_proc_re)
    # POSIX: EVERY session on the shell backend, not just the agent prefix —
    # the pty namespace (run/pty) is exclusively shells/watchers/schedule
    # sessions, and per-session hosts detach from every process tree, so a
    # full stop that does not name them leaves nothing else able to reach
    # them (schedules would keep firing on a stopped cluster). Restores the
    # pre-host full-stop semantic; whether schedule sessions deserve their
    # own stop story is an open semantic question deliberately NOT decided
    # here. Windows: the shell backend IS winproc, sharing run/sessions with
    # the service roster — an unprefixed list would sweep ava-ops/restarter/
    # watchdog (and any in-flight ava-updater) into this reap, which runs
    # BEFORE the ordered service stop. No pty shells exist there
    # (conventions/windows-setup.md), so the agent prefix — an empty or
    # agent-only set — remains the correct scope.
    if IS_WINDOWS:
        # The prefix alone still admits one service — `ava-agent-runner-watchdog`
        # starts with the agent prefix and has no digit tail — so the roster's
        # names are excluded explicitly; killing the watchdog here (before the
        # ordered stop) was an inherited scope leak of the prefix filter.
        service_names = {session_name(spec.session) for spec in build_services()}
        shell_sessions = [
            s
            for s in backend.list_sessions(prefix=prefix)
            if not proc_re.match(s) and s not in service_names
        ]
    else:
        shell_sessions = [s for s in backend.list_sessions() if not proc_re.match(s)]
    if not proc_sessions and (not kill_shells or not shell_sessions):
        return []
    if kill_shells:
        from shared.watcher_registry import killed_watcher_annotations

        for _line in killed_watcher_annotations(shell_sessions):
            print(_line)
    results: list[tuple[str, str]] = []
    # phase 1: SIGTERM every agent process, then one shared-deadline wait.
    _signal_agent_procs(native, proc_sessions, timeout_s=timeout_s)
    # Force-kill each: a survivor gets SIGKILL, a clean exit's stale record is
    # just reaped (kill_session is a noop on a dead process). Report the outcome.
    results += _kill_agent_procs(native, proc_sessions)
    if kill_shells:
        # phase 2: force-kill the agents' persistent shell / watcher sessions.
        results += _kill_agent_shells(backend, shell_sessions)
    return results


def _agent_proc_sessions(
    native: Any, proc_re: re.Pattern[str], legacy_proc_re: re.Pattern[str]
) -> list[str]:
    """The agent PROCESS sessions on this host: `ava-agent-<id>` and — from the
    transitional path-only cutover — `ava-<cluster>-agent-<id>` records (the
    native registry lives under this home, so any such record belongs to this
    cluster by construction; no service session matches either pattern)."""
    return sorted(
        s
        for s in native.list_sessions(prefix="ava-")
        if proc_re.match(s) or legacy_proc_re.match(s)
    )


def _signal_agent_procs(native: Any, proc_sessions: list[str], *, timeout_s: float) -> None:
    """SIGTERM every agent process, then wait under one shared deadline — so the
    teardown is O(slowest agent), not O(sum of agents). Survivors are force-killed
    by the caller after this returns."""
    import time

    for session in proc_sessions:
        native.graceful_signal(session)
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline and any(native.has_session(s) for s in proc_sessions):
        time.sleep(0.5)


def _kill_agent_procs(native: Any, proc_sessions: list[str]) -> list[tuple[str, str]]:
    """Force-kill each agent process after the shared-deadline wait: a survivor
    gets SIGKILL, a clean exit's stale record is just reaped (kill_session is a
    noop on a dead process). Returns (session, 'forced'|'graceful') for the stop
    output."""
    results: list[tuple[str, str]] = []
    for session in proc_sessions:
        survived = native.has_session(session)
        native.kill_session(session, graceful=False)
        results.append((session, "forced" if survived else "graceful"))
    return results


def _kill_agent_shells(backend: Any, shell_sessions: list[str]) -> list[tuple[str, str]]:
    """Force-kill the agents' persistent shell / watcher sessions, which outlive
    their processes. Returns (session, 'child') pairs for the stop output."""
    results: list[tuple[str, str]] = []
    for session in shell_sessions:
        backend.kill_session(session, graceful=False)
        results.append((session, "child"))
    return results


def _reap_cluster_chrome() -> None:
    """Finish a browser teardown the session kill could not: kill any Chrome still
    running on THIS cluster's profile.

    Called only from the `keep_browser=False` paths (`ava stop --stop-browser`,
    and `ava cluster destroy` via its `--stop-browser` child stop) — the default
    stop preserves the login Chrome and never comes here, so a normal stop pays
    nothing for this. After a `SingletonLock` handoff Chrome is no longer a
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
    graceful: bool,
) -> None:
    """The "The following will be stopped" block shown before the confirm gate."""
    # Dynamic lookup for monkeypatch-aware tests.
    import cli.commands as _ns

    print("\nThe following will be stopped:")
    print(f"  service sessions: {', '.join(service_sessions) if service_sessions else '(none)'}")
    if reap_agents:
        # Static line — enumerating agent sessions here would enumerate every
        # session before the confirm gate; the reap (and its listing) runs post-confirm.
        print("  agent sessions: graceful stop, then force-kill any straggler")
    if keep_browser and _ns._has_session(session_name(_BROWSER_SESSION)):
        print(f"  browser: kept up ({session_name(_BROWSER_SESSION)}, login session preserved)")
    if runner_only:
        print("  infra (pg/redis): skipped (agent-runner uses the central node)")
    elif keep_infra:
        print("  infra (pg/redis): kept up (cmd_update will migrate next and needs DB)")
    else:
        print("  infra (pg/redis): this cluster's own instance stopped (data preserved on disk)")
    if graceful:
        print(
            f"  mode: graceful (batch SIGTERM + one shared "
            f"{stop_timing.REAP_KILL_WINDOW_S:g}s deadline, fallback force-kill)"
        )
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


def _reap_agents_step() -> None:
    """Step 1.5: reap this cluster's agent processes (gateway-spawned, not
    ServiceSpecs — the service loop above never saw them) + their shells."""
    # Dynamic lookup for monkeypatch-aware tests.
    import cli.commands as _ns

    print("\n→ reap agent sessions")
    reaped = _ns._reap_agent_sessions()
    if reaped:
        for session, mode in reaped:
            marker = "✓" if mode in ("graceful", "child") else "⚠"
            print(f"  {marker} {session} ({mode})")
    else:
        print("  (none)")


def _do_stop(
    _repo: Path,  # used by the orphan-listener sweep (step 4); kept for call-site stability
    *,
    graceful: bool,
    require_confirmation: bool = True,
    keep_infra: bool = False,
    preserve_sessions: frozenset[str] = frozenset(),
    keep_browser: bool = True,
    reap_agents: bool = False,
    force_reap_agents: bool = False,
    announce: bool = False,
    teardown_extras: bool = False,
) -> int:
    """Shared stop implementation used by `cmd_stop` and `cmd_update`.

    announce=True (cmd_stop): POST `/api/cluster/stopping` once the stop is
        confirmed, so the roster shows a deliberate stop instead of a crash.
        It must run after the confirm gate — announcing before it stamps
        `machines.stopped_at` even when the operator aborts at the prompt,
        and nothing clears the stamp until the next `ava start`.

    graceful=False (cmd_stop default): session kill (SIGHUP force-kill), fast.
    graceful=True (used by cmd_update): signal controllers first, then batch
        SIGTERM every dependent; all targets share one absolute timeout before
        fallback force-kill, so daemon cleanup is O(slowest), never O(roster).

    require_confirmation=False: skip stdin "y/N" prompt (used by `cmd_update` streaming runs).

    keep_infra=True: do **not** stop this cluster's own pg/redis instance; keep
        it alive. Used by the `cmd_update` orchestrator — the next step
        (migrate) still needs to connect to DB; stopping the data plane
        immediately gives connect-refused on migration. `cmd_stop` defaults to
        False; manually stopping the cluster has "I want to fully stop"
        semantics, no need to keep DB.

    preserve_sessions: service sessions to leave running. `cmd_update` passes
        {'frontend'} on a backend-only change so the UI keeps serving
        across the backend restart (no pointless ~30-60s rebuild).

    keep_browser=True (default): also leave the headed browser session running
        (see _BROWSER_SESSION) — an in-place stop / update never bounces the
        login Chrome. A full teardown (`ava stop --stop-browser`, the
        cluster-down subprocess) passes False to take it down too.

    reap_agents=True (cmd_stop): also reap this cluster's agent processes
        (`ava-agent-<id>`, detached native sessions) + their shells, which
        are spawned via the gateway and so are not ServiceSpecs — the service
        stop above leaves them running otherwise (they then outlive `ava stop`
        on a stale env). `cmd_update` / `cmd_restart` leave it False: their
        agent lifecycle is the rollout's (quiesce + respawn on new code), not a
        teardown; orthogonal to keep_infra (data plane, not agents), so `ava cluster
        destroy` reaps its cluster's agents too.

    force_reap_agents=True (update only, graceful path only): CAS-mark local
        live agents restarting and include their native process sessions in
        the graceful service batch. A non-graceful stop ignores the flag.
        Their persistent PTY sessions remain live, and the agents and services
        consume the same absolute stop deadline.

    teardown_extras=True (cmd_stop): also tear down what converge registered
        OUTSIDE the service session roster — the fleet UI gate (`stop_gate_service`: launchd job on
        macOS, pidfile daemon elsewhere) and the permissions-helper LaunchAgent
        (`stop_permissions_helper`). These are not ServiceSpecs, so the service
        loop never sees them, and a plain process kill does not stick on the
        launchd-registered ones (KeepAlive respawns them). `cmd_update` /
        `cmd_restart` pass False: the gate must keep the entry port live through
        a rollout by construction.

    role-aware: agent-runner skips the pg/redis stop (host has no local
    data plane anyway). When role is unset ('unknown'), conservatively take
    the gateway path — `ava stop` does not depend on completed setup.
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
        graceful=graceful,
    )
    if not _confirm_stop(require_confirmation=require_confirmation):
        return 0

    # A deliberate stop must revoke the prior start's recovery authority before
    # any daemon has a chance to observe its own shutdown or a dead peer.
    from shared import start_serving

    start_serving.clear_serving()

    if announce:
        _announce_stopping()

    # One force-update budget covers both agent stragglers and services. Mark
    # the agents before signalling anything so the paused restarter will bring
    # them back on new code; their persistent PTY sessions remain out of scope.
    stop_deadline = time.monotonic() + stop_timing.REAP_KILL_WINDOW_S if graceful else None
    # force_reap_agents is an update-only knob: the non-graceful stop path kills
    # service sessions outright and never enters the include_agent_processes
    # branch, so honouring the flag there would CAS-mark agents without any
    # process kill to follow (QA nit 2). The force_reap stage keeps its
    # telemetry marker for the update path (QA nit 1, #777 contract).
    if force_reap_agents and graceful:
        with updater_stage("force_reap"):
            _ns._force_reap_local_agents(defer_process_stop=True)

    # 1) stop service sessions (and force-update agent processes when asked).
    # Controllers are signalled first, every target shares stop_deadline, and
    # the data plane remains a later step exactly as before.
    _stop_sessions(
        service_sessions,
        graceful=graceful,
        include_agent_processes=force_reap_agents and graceful,
        deadline=stop_deadline,
    )

    # 1.4) a teardown that asked for the browser down finishes the job: kill any
    # Chrome still running on THIS cluster's profile. The session kill above
    # cannot reach a Chrome that left the tree on a `SingletonLock` handoff, and
    # such a Chrome holds the cluster's CDP port against the next launch
    # (services/browser/orphan.py carries the identification argument). Placed
    # after step 1 on purpose: the watchdog is dead by now, so nothing relaunches
    # Chrome onto the port we just cleared.
    if not keep_browser:
        _ns._reap_cluster_chrome()

    # 1.5) reap agent processes (spawned via the gateway, not ServiceSpecs, so the
    # loop above never saw them). Graceful C-c + wait, then force-kill stragglers.
    if reap_agents:
        _reap_agents_step()

    # 2) stop the data plane (data persists on disk).
    _stop_data_plane(skip_infra=skip_infra, runner_only=runner_only)

    # 3) full-stop extras: tear down what converge registered outside the service roster —
    # the entry-port gate and the permissions-helper LaunchAgent. After the
    # sessions are dead nothing can relaunch them (no watchdog survives), so
    # this is the last step. cmd_update / cmd_restart never come here.
    if teardown_extras:
        from cli.commands._stop_extras import (
            stop_gate_service,
            stop_permissions_helper,
        )

        stop_gate_service()
        stop_permissions_helper()

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


def cmd_stop(
    *, keep_infra: bool = False, require_confirmation: bool = True, stop_browser: bool = False
) -> int:
    """Default force-kill (session kill sends SIGHUP). Graceful path goes
    through `ava cluster update` or `_do_stop(graceful=True)` explicitly.

    `ava stop` stays local — it tears down the gateway + this cluster's own pg/redis
    instance on this host, so it cannot delegate to the gateway (which would die before
    responding and cannot stop the data plane it runs on). Once the stop is confirmed it
    best-effort announces the intentional shutdown to the gateway so the
    cluster view shows this host as "stopped" rather than "offline" (a live
    probe cannot tell a deliberate stop from a crash).

    keep_infra / require_confirmation are set by `ava stop --keep-infra -y`; the
    internal cluster-down helper (`cmd_cluster_down`, reached via `ava cluster
    destroy`) uses both so it stops only the cluster's service sessions and never
    tears down the Postgres/Redis instance out from under a migrate. It also sets stop_browser=True so a
    full cluster teardown does not leave an orphan headed Chrome.

    stop_browser=False (default): the headed browser session is left running so
    its login Chrome is not bounced (see _BROWSER_SESSION). `ava stop
    --stop-browser` flips it to also take the browser down.
    """

    from shared import maintenance

    maintenance.require_released("force stop")
    repo = _repo_root()
    print(f"[ava stop] cwd = {repo}")
    return _do_stop(
        repo,
        graceful=False,
        require_confirmation=require_confirmation,
        keep_infra=keep_infra,
        keep_browser=not stop_browser,
        reap_agents=True,
        announce=True,
        teardown_extras=True,
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
    from shared.cluster_lock import update_lock_holder
    from shared.host_deploy_state import read

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

    Designed for the detached updater session, where interactive confirmation
    of stop is not desirable and the start step runs without a tty.

    `quiesce` (the updater's per-host self-heal path): pause this host's
    restarter, signal its live agents to restart (system:update) and wait per
    `mode`; stragglers are force-reaped in the separate force_reap stage that
    follows, so the quiesce stage itself never exceeds the mode's budget — the
    same agent-drain contract a rollout's stop-the-world holds, so a
    watchdog/standalone update no longer leaves every agent running old code.
    `force_reap` (the rollout's Phase-B backstop): no signal, no wait — mark this
    host's still-live agents 'restarting' and kill them (the gateway-side
    quiesce already drained them; this catches the stragglers it timed out on).

    Returns 0 on full success; `RESTART_DECLINED_EXIT_CODE` when the preflight
    refused and nothing was stopped (the host is untouched and still serving);
    any other non-zero from the first failing step *after* the stop, which means
    the host may be down. The caller — including the detached updater shell — must
    keep those two apart: only the second one wants an `ava start` recovery.

    `SERVICES_NOT_READY_EXIT_CODE` is one of those "other" codes and is inherited
    straight from `_cmd_start_body`: the stop happened, the start ran every step, and
    a service came back not serving. It lands in both updater ladders' recovery branch
    (`>= RESTART_DECLINED_EXIT_CODE + 1`), which is the right branch — `ava start` is
    idempotent and will relaunch whatever session is missing.
    """
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

    # Agent drain before the stop: quiesce (signal + wait per mode) for a
    # standalone self-heal; force-reap for the rollout's Phase-B backstop and
    # for the quiesce's own stragglers. The reap is its own stage, never part
    # of the quiesce window — the quiesce stage must stay within the mode's
    # budget, and the reap's SIGTERM grace is escalation time, not drain time
    # (the quiesce stage ran 25.6s = budget 10s + reap 15s before the split,
    # Task #2055). Both mark survivors 'restarting' so the restarter — paused
    # by the quiesce itself, resumed by the start below — respawns them on new
    # code.
    drained = True
    if quiesce:
        with updater_stage("quiesce"):
            drained = _ns._quiesce_local_agents(mode)
    if force_reap or (quiesce and not drained):
        with updater_stage("force_reap"):
            _ns._force_reap_local_agents()

    # keep_infra=True: an internal restart bounces this host's service sessions,
    # never this cluster's own pg/redis instance — stopping the data plane mid-orchestration
    # kills the gateway orchestrator's own DB polling (same failure mode as
    # the self-update leg; see _run_agent_runner_self_update).
    with updater_stage("stop"):
        rc = _ns._do_stop(repo, graceful=False, require_confirmation=False, keep_infra=True)
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
