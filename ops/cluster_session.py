"""Session + orchestration-liveness primitives every cluster op stands on.

The leaf of the `ops/cluster*` family: it starts and probes the detached sessions
that a pause, a status read and a deploy all need, and it imports none of them.
`ops/cluster.py` re-exports this module; nothing here imports that facade, which
is what keeps the family acyclic.

Every session this module starts or stops goes through the platform session
backend. Stops already did (`pause_local_cluster` -> `get_backend().kill_session`);
the four *spawn* sites (`unpause_local_cluster`, `spawn_update`, `spawn_rollout`,
`spawn_restart`) share `_spawn_detached_session`, which launches on
`get_backend()` — the same backend that hosts services (POSIX: the native
process supervisor; Windows: winproc). S7 moved the orchestration sessions
(updater / rollout / cluster-restart) onto it, retiring the last legacy backend in
the cluster path. The Windows half of that story is why the helper exists at
all: before it, a Windows agent-runner could be paused but never resumed and
could never run its own `ava cluster update` — every self-heal died on a
missing-backend error, so no drift dimension (schema or pin) could ever converge and
the restarter stayed dead. One-way reachability like that is worse than no
reachability, hence one helper rather than four branches.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Literal, cast

from shared.cluster_lock import DeployLease
from shared.host_deploy_state import HostDeployState


class OrchestrationSpawnFailed(RuntimeError):  # noqa: N818 — state description, same style as ClusterUpdateInProgress
    """The session backend declined to start an orchestration session.

    The gateway surfaces this as a 503 — a transient infra condition, the same
    class the old missing-backend error reported, minus the legacy dependency.

    ``started`` is False only when the backend definitively returned a decline.
    None means launch outcome is ambiguous: fork/Popen may have succeeded before
    session-record bookkeeping raised, so callers must not undo child-owned
    lifecycle state based on the exception alone.
    """

    def __init__(self, message: str, *, started: bool | None = None) -> None:
        super().__init__(message)
        self.started = started


_log = logging.getLogger(__name__)
_UNSET = object()

# Repo root: gateway/cluster.py -> gateway -> repo root
_REPO_ROOT = Path(__file__).resolve().parent.parent

# Bare service names — composed into real session names by
# shared.cluster.session_name (`ava-<service>`).
_RESTARTER_SERVICE = "restarter"
_UPDATER_SERVICE = "updater"
_ROLLOUT_SERVICE = "rollout"
_ROLLOUT_DRYRUN_SERVICE = "rollout-dryrun"
_CLUSTER_RESTART_SERVICE = "cluster-restart"

# What an in-flight orchestration is, as a status consumer (the Settings panel)
# shows it. Sourced from the deployment-state lease's `kind` (rollout/restart)
# and the local updater lease (`update`) — the explicit-model replacement for
# probing session names (R1 old-signal sweep, PR5).
OrchestrationKind = Literal["rollout", "restart", "update"]

# Whole-cluster orchestration sessions, paired with the kind a spawn refusal
# reports. The spawn-path mutex (`cluster_deploy._assert_no_orchestration_in_flight`)
# still probes the session backend: the lease cannot see the watchdog-spawned,
# lease-less updater until its claim lands, and the probe is the first line
# against racing spawns (the DB compare-and-set is the authoritative second).
# The status READ (`current_orchestration`) no longer uses this — it reads the DB.
# A rollout dry-run is deliberately excluded: it is informational and non-mutating,
# so it never blocks a real deploy or gets interrupted by ops-manager orchestration actions.
_ORCHESTRATION_KINDS: tuple[tuple[str, OrchestrationKind], ...] = (
    (_ROLLOUT_SERVICE, "rollout"),
    (_CLUSTER_RESTART_SERVICE, "restart"),
    (_UPDATER_SERVICE, "update"),
)


def _native_arg(value: str) -> str:
    """Quote `value` as one argument of a `cmd /c` command line.

    cmd.exe has no `shlex.quote` equivalent — `^`-escaping is context-dependent
    and silently wrong in half of them. Inside a double-quoted run, though, every
    operator that could change the command's shape (`& | < > ^`) is already
    literal, so double-quoting is the whole job. The two characters that cannot
    be represented at all — an embedded `"` and a newline — are refused instead of
    mangled. Nothing this module interpolates (a commit sha, a branch name, a
    trigger label like `agent:9`) can legitimately contain either.
    """
    if any(c in value for c in '"\r\n\0'):
        raise ValueError(f"cannot pass {value!r} through a cmd.exe command line")
    return f'"{value}"'


def _spawn_detached_session(session: str, *, shell_cmd: str, native_cmd: str) -> None:
    """Start `session` as a detached background session on the platform's session
    backend — the same backend that hosts services (POSIX: the native process
    supervisor, `get_backend()`; Windows: winproc). The two commands are the same
    work in two shells:

    - **shell_cmd (POSIX)** — run through the backend's `bash -lc` wrapper,
      which re-activates this checkout's venv inside the command (`ava` /
      `python` resolve from the venv, never a login PATH — the rc=127 lesson in
      `spawn_rollout`). The command's own `cd <repo>`, its `2>&1 | tee` pipeline
      and its `[session-exit] rc=` verdict survive unchanged.
    - **native_cmd (Windows)** — `cmd /c` via `shared.winproc`: no login shell,
      no POSIX `if/then/else`, so `native_cmd` is the plain `&&` / `||` chain.

    The full child env — including the venv-activated PATH that resolves `ava` —
    rides `forward_env_dict()` as the child's real environment: the backend hands
    env out-of-band, so nothing secret ever lands on an argv (issue #974; the
    0600 env-file handoff the legacy path needed is gone with it).

    Raises:
        OrchestrationSpawnFailed: the backend declined to start the session
            (or the launch itself failed).
    """
    from shared.platform_backend import get_backend as get_platform_backend
    from shared.session_backend import get_backend
    from shared.session_env import forward_env_dict

    # The command flavor is a platform property: POSIX hosts run the shell
    # spelling, Windows runs the cmd.exe spelling. `is_posix()` is the
    # platform backend's POSIX-capability flag.
    cmd = native_cmd if not get_platform_backend().is_posix() else shell_cmd
    try:
        # exec_cmd=False: unlike a daemon session, the wrapper shell here IS the
        # session — it owns the `tee` pipeline and writes the `[session-exit] rc=`
        # verdict after the command returns, so it must outlive it.
        ok = get_backend().new_session(
            session, cmd, _REPO_ROOT, env=forward_env_dict(), exec_cmd=False
        )
    except Exception as exc:
        raise OrchestrationSpawnFailed(
            f"session backend could not confirm orchestration session {session!r}: {exc}",
            started=None,
        ) from exc
    if not ok:
        raise OrchestrationSpawnFailed(
            f"session backend could not start orchestration session {session!r}",
            started=False,
        )


def _has_orchestration_session(session: str) -> bool:
    """True if an ORCHESTRATION session (updater / rollout / cluster-restart)
    with the given name is currently alive.

    Orchestration sessions live on the session backend since S7 — the same
    backend as services (native supervisor on POSIX, winproc on Windows) —
    never the shell backend.

    Delegates to the platform session backend.
    """
    from shared.session_backend import get_backend

    return get_backend().has_session(session)


def live_orchestration_session() -> str | None:
    """Return this host's first live orchestration session, if any.

    Action/recovery paths need the process fact during the narrow spawn -> DB
    lease-acquire window. ``current_orchestration`` intentionally projects DB
    leases for status and therefore cannot close that lifecycle gap.
    """
    import shared.cluster

    for service, _kind in _ORCHESTRATION_KINDS:
        session = shared.cluster.session_name(service)
        if _has_orchestration_session(session):
            return session
    return None


def current_orchestration(
    state: HostDeployState | None | object = _UNSET,
    lease: DeployLease | None | object = _UNSET,
) -> OrchestrationKind | None:
    """The whole-cluster orchestration running on this host, or None if idle.

    Returns 'rollout' / 'restart' / 'update' — the value the status panel reads
    to keep the Update / Restart actions disabled (and show a progress label)
    for the whole multi-minute orchestration, not just the request blink.

    R1 old-signal sweep (PR5): this is a DB read now, not a session probe —
    the deployment-state lease's `kind` while an orchestration executes (a
    settle hold has a note and reads as idle), and `update` while this host's
    updater lease is live (the lease-less watchdog-spawned updater, which the
    lease's kind cannot see). A stale `converging` row with an expired lease
    reads as None — the same way a dead session did — so heal paths that
    refuse on this value still run for genuinely stranded hosts.

    This is a status read: any read failure degrades to None (no orchestration
    visible) rather than raising. The action-trigger path keeps the strict
    `_assert_no_orchestration_in_flight` session check, where a session that
    cannot be inspected is a real hard failure.
    """
    try:
        if lease is _UNSET:
            from shared.cluster_lock import read_update_lease

            resolved_lease = read_update_lease()
        else:
            resolved_lease = cast(DeployLease | None, lease)
        if (
            resolved_lease is not None
            and resolved_lease.kind is not None
            and resolved_lease.note is None
        ):
            return resolved_lease.kind
        if state is _UNSET:
            from shared.host_deploy_state import read as read_host_deploy_state

            resolved_state = read_host_deploy_state()
        else:
            resolved_state = cast(HostDeployState | None, state)
        if resolved_state is not None and resolved_state.updater_live:
            return "update"
        return None
    except Exception:
        return None
