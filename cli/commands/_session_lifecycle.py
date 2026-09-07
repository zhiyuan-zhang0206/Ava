"""Session lifecycle helpers shared across `start` / `stop` / `restart`.

Three primitives:
- `_has_session` — existence check (idempotent guard)
- `_new_session` — start a long-running command in a detached session
- `_kill_session` / `_graceful_kill_session` — stop, force vs graceful

`_launch_sessions` composes them per `_services_for_roles_annotated` (which
also surfaces gated-out services + why) + skip set. Its idempotence guard asks the
service's probe, not just the session backend: an existing session is a necessary
but not sufficient condition for "already running", and believing it is sufficient
is what let a start skip a gateway that was not there (`_probe._husk_session_reason`).

A session that could not be created is reported, not discarded. It used to be:
`_new_session` printed one cross and its bool was dropped on the floor, so a
launch refusal reached no caller and no exit code. Prod's 2026-08-06
frontend-only rollout is what that costs — a `✗ failed to start ava-frontend`
line, `[session-exit] rc=0`, and a dark UI nobody was told about. So the launch
is retried once (these refusals are overwhelmingly transient — a husk the kill
raced, a momentarily unreachable backend) and whatever still will not start
comes back in `LaunchOutcome.failed`.

All primitives delegate to ``shared.session_backend``, which selects the
platform-appropriate implementation (native process supervisor on POSIX,
winproc on Windows).
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import NamedTuple

from cli.commands._repo import (
    ServiceSpec,
    _ensure_frontend_deps,
    _services_for_roles_annotated,
    session_name,
)
from ops.service_spec import profile_marker
from shared.cluster.derive import runner_db_url_projection
from shared.config import settings
from shared.machine import MachineRoles


class LaunchOutcome(NamedTuple):
    """What a launch pass did: the roster it covers, and what would not start.

    `started` is the roster the caller gates readiness on (unchanged meaning:
    every service this host should be running, including the ones already up).
    `failed` holds the session names that could not be created — a strictly
    stronger fact than "not ready yet", since nothing was spawned to become
    ready. It is a separate field rather than a subtraction from `started`
    because the readiness gate and the launch verdict answer different questions
    and must not be collapsed into one list.
    """

    started: tuple[ServiceSpec, ...]
    failed: tuple[str, ...]


def _has_session(session: str) -> bool:
    """True if a session with ``session`` is currently alive.

    Delegates to the platform backend (native supervisor / winproc.has_session).
    """
    from shared.session_backend import get_backend

    return get_backend().has_session(session)


def _new_session(
    session: str, cmd: str, cwd: Path, *, extra_env: dict[str, str] | None = None
) -> bool:
    """Launch ``cmd`` as a detached, named background session.

    On POSIX wraps in ``bash -lc`` so user-local PATH additions (e.g.
    ``~/.local/bin/uv``) are visible; on Windows the native supervisor inherits
    the caller's full env and reaches for ``cmd /c`` only when the command's own
    syntax needs a shell — a plain daemon command is launched directly, because
    a detached ``cmd.exe`` swallows its children's output
    (``shared.winproc._plan_launch``).

    Env forwarding uses the daemon policy (``forward_env_dict``); the child
    decides its config source by its own role at Settings build. The
    settings-lite ``AVA_CONFIG_FETCH=skip`` opt-out of maintenance verbs is
    deliberately never forwarded either (AVA_CONFIG_SOURCE is gone).

    ``extra_env`` is layered onto the forwarded env at the end (wins over
    any existing key).
    """
    from shared.session_backend import get_backend
    from shared.session_env import forward_env_dict

    env = forward_env_dict()
    if extra_env:
        env.update(extra_env)
    ok = get_backend().new_session(session, cmd, cwd, env=env)
    if ok:
        # `running_sha` was recorded immediately before this launch pass. Unlike
        # that host-wide update baseline, the session-code sidecar is bound to
        # this one process identity and can therefore answer whether THIS live
        # daemon needs replacing after a checkout advances.
        from shared.running_sha import get as running_sha
        from shared.session_code import record_launch

        sha = running_sha()
        if sha is not None:
            record_launch(session, sha)
    if not ok:
        print(
            f"  \u2717 could not start session {session}",
            file=sys.stderr,
        )
    return ok


def _kill_session(session: str, *, expected: bool = False) -> bool:
    """Force-kill a session by name (backend force-kill).

    Idempotent — returns True even if the session is already absent.
    ``expected`` marks an operator-initiated transition (update/stop/start
    husk-clear): there the force-kill escalation is the designed remedy, so the
    backend logs it at INFO instead of WARNING/ERROR.

    Service sessions are hosted on the service backend only (native supervisor
    on POSIX, winproc on Windows) — orchestration sessions (updater/rollout)
    live on a separate backend and are never touched here.
    """
    from shared.session_backend import get_backend

    ok, _mode = get_backend().kill_session(session, graceful=False, expected=expected)
    return ok


def _graceful_kill_session(
    session: str, timeout_s: float = 15.0, *, expected: bool = False
) -> tuple[bool, str]:
    """Graceful stop: send interrupt, wait for clean exit, force-kill on timeout.

    On POSIX sends SIGTERM to the session's process tree (the native
    supervisor's graceful path); on Windows sends Ctrl-Break via winproc.

    Returns ``(ok, mode)`` — mode in ``{'graceful', 'forced', 'noop'}``.

    ``expected`` marks an operator-initiated transition (update/stop): the
    timeout-then-force escalation is the designed remedy there, so the backend
    logs it at INFO instead of WARNING/ERROR.

    Uses dynamic lookup (``import cli.commands as _ns``) so tests can
    monkeypatch ``_has_session`` / ``_kill_session`` and this
    function picks up the mocks.
    """
    import cli.commands as _ns

    if not _ns._has_session(session):
        return True, "noop"

    # Graceful stop via the backend (handles POSIX + Windows internally).
    from shared.session_backend import get_backend

    ok, mode = get_backend().kill_session(
        session, graceful=True, timeout=timeout_s, expected=expected
    )
    return ok, mode


def _is_stop_controller(session: str) -> bool:
    """Controllers that can respawn another service while stop is in flight."""
    return session.endswith("watchdog")


def _stop_sessions(sessions: list[str]) -> None:
    """Force-stop the selected roster services after explicit interruption consent.

    Persistent terminals have their own backend and full-stop scope. Normal
    pause/stop uses the verified no-escalation boundary in _maintenance_stop.
    """
    import cli.commands as _ns

    print("\n→ kill sessions")
    ordered = [s for s in sessions if _is_stop_controller(s)] + [
        s for s in sessions if not _is_stop_controller(s)
    ]
    for session in ordered:
        ok = _ns._kill_session(session, expected=True)
        print(f"  {'✓' if ok else '✗'} {session}")
        if not ok:
            raise RuntimeError(f"force stop did not end session {session}")


def _launch_roster(roles: MachineRoles, skip: set[str]) -> tuple[ServiceSpec, ...]:
    """The specs a launch with this role set and skip set would start — no side effects.

    `_launch_sessions` narrows to exactly this set before touching anything,
    and it is the same set `ava start`'s pre-bind health-port gate has to check;
    the gate runs first, so the answer has to be reachable without launching.
    Derived from `_services_for_roles_annotated` rather than copied, so a gate a
    service carries (browser's missing $DISPLAY, task-maintenance's toggle) keeps
    one meaning across both callers.
    """
    return tuple(
        spec
        for spec, reason in _services_for_roles_annotated(roles)
        if reason is None and spec.session not in skip
    )


def _service_extra_env(spec: ServiceSpec) -> dict[str, str]:
    """Profile and database projection required by one service session."""
    extra: dict[str, str] = {}
    marker = profile_marker(spec)
    if marker is not None:
        extra["AVA_PROCESS_PROFILE"] = marker
    if marker == "agent":
        extra["AVA_DB_URL"] = runner_db_url_projection(settings.data_plane.db_url)
    return extra


def _relaunch_once(sess: str, cmd: str, cwd: Path, extra: dict[str, str] | None) -> bool:
    """One retry of a `new-session` that just failed; True when the session is up.

    Kills any half-created session of that name first — the backend refuses a
    duplicate name, so a first attempt that failed *after* claiming the name
    would make the retry fail for a second, unrelated reason and hide the real one.
    """
    import cli.commands as _ns

    print(f"  ↻ {sess}: retrying the launch once", file=sys.stderr)
    _ns._kill_session(sess, expected=True)
    return _ns._new_session(sess, cmd, cwd, extra_env=extra)


def _stale_session_code_reason(session: str, spec: ServiceSpec) -> str | None:
    """Why a healthy prod service must be relaunched for code drift, or ``None``.

    A session version is actionable only when both sides are known: the live
    daemon's frozen SHA (or, when it has no health endpoint, its launch sidecar)
    and the installed prod source's HEAD. Dev worktrees compare different trees
    by design, and an executing update or local orchestration owns the normal
    checkout-ahead transient, so all of those cases preserve ordinary idempotent
    start behavior.
    """
    from ops.controllers._deploy_state import read_lease_state, read_orchestration
    from shared.cluster_drift import prod_source_head_sha, running_from_prod_source
    from shared.session_code import launched_sha

    if not running_from_prod_source():
        return None
    head = prod_source_head_sha()
    health_url = spec.curl_url if spec.curl_url and spec.curl_url.endswith("/healthz") else None
    launched = launched_sha(
        session,
        service=spec.session.replace("-", "_"),
        health_url=health_url,
    )
    if head is None or launched is None or launched == head:
        return None

    # Match the code controller's narrow settle-hold rule: an executing rollout
    # owns the transition, while a hold waiting for this host is specifically
    # waiting for the convergence this relaunch provides.
    lease = read_lease_state(settle_hold_mode="narrow")
    if lease.kind in {"unreadable", "executing"} or (
        lease.kind == "settle_hold" and not lease.waits_for_this_host
    ):
        return None
    orchestration = read_orchestration()
    if orchestration.kind != "none":
        return None
    return f"session launched on {launched[:7]}; prod source is {head[:7]}"


def _launch_sessions(roles: MachineRoles, skip: set[str], repo: Path) -> LaunchOutcome:
    """Launch the sessions this host's capability set needs (+ --disable-service).

    --disable-service use case: on a dev machine with no display, skip
    ava-browser so the watchdog does not keep respawning a daemon that cannot
    start.

    Returns a `LaunchOutcome`: the services it launched (incl. ones already
    running) so the caller can wait for their probes before snapshotting status
    (`_wait_for_services_ready`), plus the session names that would not start
    even after one retry. Both halves matter to the caller and neither implies
    the other — a service can be in `started` and still be unready, and one in
    `failed` never got far enough to be probed at all.
    """
    # Dynamic lookup so tests can monkeypatch `cli.commands._has_session`
    # / `cli.commands._new_session`. The sub-module's own
    # `_has_session` binding is otherwise frozen at import time.
    import cli.commands as _ns

    print("\n→ sessions")
    # Surface config/capability-gated services + WHY (e.g. ava-browser dropped for
    # a missing $DISPLAY on headed Linux), so the roster never silently shrinks.
    for spec, reason in _services_for_roles_annotated(roles):
        if reason is not None:
            print(f"  - {session_name(spec.session)} skipped: {reason}")
    if skip:
        print(f"  (skip via --disable-service: {sorted(skip)})")
    services_to_start = _launch_roster(roles, skip)
    failed: list[str] = []
    for spec in services_to_start:
        sess = session_name(spec.session)
        if _ns._has_session(sess):
            husk = _ns._husk_session_reason(spec)
            if husk is None:
                stale_code = _stale_session_code_reason(sess, spec)
                if stale_code is None:
                    print(f"  ✓ {sess} already running")
                    continue
                print(
                    f"  ⚠ {sess} session is alive but runs stale code ({stale_code}) — relaunching"
                )
            else:
                # The session outlived its service. Clear it before relaunching:
                # `new-session -d -s <name>` refuses a duplicate name, so skipping the
                # kill would turn the husk into a launch failure instead of a launch.
                print(f"  ⚠ {sess} session is alive but the service is not ({husk}) — relaunching")
            if not _ns._kill_session(sess, expected=True):
                # NOT a launch failure: the session is still there, and the husk
                # verdict came from a single probe that a merely-slow service can
                # fail. The readiness gate polls it properly and is the right judge
                # — calling it a failed launch here would fail starts whose service
                # was alive the whole time.
                print(
                    f"  ✗ {sess}: could not clear the stale session — NOT relaunched. "
                    "The readiness gate below will report this service unready.",
                    file=sys.stderr,
                )
                continue
        if spec.session == "frontend":
            _ensure_frontend_deps(repo)
            print(f"  + {sess} (build ~30-60s)")
        else:
            print(f"  + {sess}")
        if spec.before_launch is not None:
            spec.before_launch()
        # Determine process profile from the service's spec (ops.service_spec.profile_marker):
        # gateway daemons get AVA_PROCESS_PROFILE=gateway, agent-runner daemons get
        # =runner; a spec with no_profile_marker (labeler) or with both capabilities
        # gets none. The profile drives per-process env assembly in
        # shared.dotenv_boot._enforce_cluster_env_authority — the gateway pop
        # removes agent-runner cluster keys (LLM provider keys) from os.environ,
        # which is exactly why the labeler opts out (it builds chat models).
        extra = _service_extra_env(spec)
        if not _ns._new_session(
            sess, spec.cmd, repo, extra_env=extra or None
        ) and not _relaunch_once(sess, spec.cmd, repo, extra or None):
            failed.append(sess)
    return LaunchOutcome(services_to_start, tuple(failed))
