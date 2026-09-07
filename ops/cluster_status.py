"""This host's snapshot in the multi-machine view.

Assembles `ClusterStatus` from local probes — pidfiles for the daemons, the
session backends' records for the services and the agents' persistent shells,
the database's local agent identities — and
answers the one endpoint that stays readable while the cluster is paused.

  Observability: GET /api/cluster/status — bypasses 503 mode, always
    returns this host's state directly.
"""

from __future__ import annotations

import logging
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel

import shared.cluster
import shared.cluster_lock
import shared.db
import shared.host_deploy_state
from ops import cluster_pause, cluster_session
from ops.cluster_session import OrchestrationKind
from ops.rpc_schemas import AgentSessionGroup, SessionInfo, ShellInfo
from ops.updater_outcome import UpdaterOutcome, last_updater_outcome
from shared.config import cluster_tz
from shared.machine import is_agent_runner, is_gateway, is_observability_station, machine_name
from shared.proc import process_alive
from shared.resource_sample import ResourceSample

_log = logging.getLogger(__name__)

# `connection(timeout=...)` overrides the pool's long-lived default for this
# probe. Status must degrade before the gateway's probe deadline when the
# central DB is down instead of queueing behind the pool for its full timeout.
#
# The bound covers the pool QUEUE only: once a connection is handed out, a
# query hung on a blackholed flow waits out the connection's own keepalive +
# statement ceiling (~60s, PG_KEEPALIVE_KWARGS / statement_timeout) before
# failing — same failure class as the pre-change fresh connects (connect
# timeout 5s, then the same ~60s query ceiling), so a blackhole slows the
# probe but never hangs it.
_POOL_BORROW_TIMEOUT_S = 2.0


class ClusterStatus(BaseModel):
    """`GET /api/cluster/status` response body — this host's snapshot in the
    multi-machine view.

    `ava cluster status` CLI and the future monitoring page consume the
    same payload.
    """

    machine_name: str
    # Three orthogonal capability flags (any combination on a single host) —
    # never a single categorical "role". See shared/machine.py.
    # serve_observability_station defaults False so a client on pre-station
    # code still parses a station host's snapshot.
    serve_gateway: bool
    serve_agent_runner: bool
    serve_observability_station: bool = False
    paused: bool
    # The whole-cluster orchestration alive on this host ('rollout' / 'restart' /
    # 'update'), or None when idle. This endpoint bypasses the cluster-paused 503
    # middleware, so it is the one status source readable *during* a pause — which
    # lets a consumer tell a normal in-flight rollout (paused + orchestration set,
    # wait) from a stranded pause (paused + orchestration None, a hard-killed
    # rollout left the flag; recoverable via /api/cluster/recover).
    current_orchestration: OrchestrationKind | None = None
    # How this host's last updater session ended, when this host is paused and the
    # log speaks for the *current* update — or freshly-idle within the
    # no-progress window, so a just-converged host's COMPLETED stage breakdown
    # rides the probe that saw it resume (Task #1820); None otherwise.
    #
    # It rides beside `paused` / `current_orchestration` because those two are what
    # produce the rollout poll's `POLL_STALLED` verdict, and that verdict is exactly
    # where the reason was missing: a preflight that refused (host untouched, still
    # serving) and an updater that died mid-flight (checkout moved, processes not)
    # both read as "reachable, still paused, no updater running". The orchestrator
    # already has this response in hand at the moment it decides, so the reason
    # arrives with the verdict rather than needing a second dial.
    last_updater_outcome: UpdaterOutcome | None = None
    # This host's prod-source HEAD commit (`$AVA_HOME/source`), or None when it
    # cannot be read (no prod source / git unavailable). Compared against the
    # cluster pin (`cluster_target_sha`) to surface a node drifted off the
    # cluster's pinned commit; threaded to the roster so the multi-machine view
    # shows per-node drift.
    head_sha: str | None = None
    # The commit the process answering this probe actually loaded, frozen at its
    # own boot (`shared.process_sha`), or None when it never froze one. Distinct
    # from head_sha: head_sha is the checkout the pin is compared against,
    # running_sha is code the live process holds. They differ when the checkout
    # advanced (git pull / rollout) but the process was not restarted — the roster
    # shows a node "on pin ✓" that is nonetheless running stale code.
    #
    # This speaks only for the answering process (the ops daemon on an
    # agent-runner, the gateway on a pure gateway). A sibling daemon respawned at
    # a different commit is not covered here; its own commit is on its
    # `/healthz`, and `probe_daemon` surfaces it per daemon.
    running_sha: str | None = None
    # This host's live agent shell-session count, surfaced per-machine in
    # the status panel. 0 on a host with no agents (e.g. a pure gateway).
    shell_count: int = 0
    # Per-host daemon liveness (pidfile + signal). None = could not probe. Shown
    # per-machine in the roster; central-only daemons (labeler/memory-indexer) are not
    # here — they live in the gateway services panel.
    agent_host_online: bool | None = None
    watchdog_online: bool | None = None
    # Agent-runner detail surfaced on the Status Page. `agent_count` is this
    # host's non-terminated agent identities, including idle and paused agents.
    # `session_count` counts live service and persistent terminal sessions.
    agent_count: int = 0
    session_count: int = 0
    # The agents' shell/watcher sessions grouped by agent for hierarchical
    # display (the agent process itself is not a session, so it is not a group entry).
    agent_groups: list[dict[str, object]] = []
    # This machine's live CPU / memory / disk reading — one sample, no history
    # (Prometheus holds the series). None when psutil is unavailable.
    resource: ResourceSample | None = None


def _check_pidfile(pidfile_path: str) -> tuple[bool, int | None]:
    """Read a pidfile + `process_alive(pid)` to test liveness. Returns (alive, pid).

    Checks the given path first, then falls back to the legacy
    $AVA_HOME/<name>.pid location (the parent directory of a `run/...` path)
    for backward compat during the transition to the run/ subdirectory.

    Missing/empty/non-int file -> (False, None). Pidfile present but the process
    is gone -> (False, pid)."""
    pf = Path(pidfile_path)
    # Build a list of paths to check: the canonical path, plus a legacy
    # fallback if the canonical path lives under a "run" directory.
    paths = [pf]
    if pf.parent.name == "run":
        legacy = pf.parent.parent / pf.name
        if legacy != pf:
            paths.append(legacy)
    for path in paths:
        try:
            pid = int(path.read_text().strip())
        except (FileNotFoundError, ValueError):
            continue
        if process_alive(pid):
            return True, pid
        return False, pid
    return False, None


def _count_agent_shells(sessions: list[SessionInfo]) -> int:
    """Count agent shell + watcher sessions from the session list."""
    return sum(1 for s in sessions if re.search(r"-agent-(\d+)-shell-", s.name))


def _count_local_agents(conn: Any) -> int:
    """Count this machine's retained agent identities through the snapshot connection."""
    row = conn.execute(
        "SELECT count(*) FROM agents_meta WHERE machine=%s AND status<>'terminated'",
        (machine_name(),),
    ).fetchone()
    if row is None:
        raise RuntimeError("local agent count returned no row")
    return int(row[0])


def _group_agent_sessions(
    sessions: list[SessionInfo],
) -> list[AgentSessionGroup]:
    """Group cluster sessions by agent ID into a structured list for the frontend.

    Each group is an AgentSessionGroup (agent_id, label, shells). Only the
    agent's shell/watcher sessions appear — the agent process itself is not a
    session — so a group forms for every agent that has at least one shell. The label is an empty string for now (resolved later via
    agents_meta lookup if available).
    """
    groups: dict[int, AgentSessionGroup] = {}
    for s in sessions:
        m = _AGENT_SESSION_RE.search(s.name)
        if m is None:
            continue
        agent_id = int(m.group(1))
        if agent_id not in groups:
            groups[agent_id] = AgentSessionGroup(agent_id=agent_id, label="", shells=[])
        groups[agent_id].shells.append(s)
    return list(groups.values())


# Session name prefix — sessions are named `ava-<service>` (see
# shared/cluster.py:session_name; the per-home session-record namespace already
# scopes them to this cluster). The prefix filter drops non-ava sessions.
_CLUSTER_SESSION_PREFIX = f"{shared.cluster.session_name('')}"  # "ava-"

# A bare agent main-process session (`ava-agent-<id>`). Agent processes are
# pid records, not sessions — `_collect_sessions` filters them out so the
# session count / agent groups stay session-shaped (a P1 regression in #2330
# put them back in and ghosted 26 of 38 agent_groups).
_AGENT_PROCESS_RE = re.compile(
    r"^ava-(?:[a-z0-9]+-)*(?:agent-\d+|boot-\d+-(?:\d+-\d+|[a-f0-9]{32}))$"
)

_AGENT_SESSION_RE = re.compile(r"-agent-(\d+)")
# Parse one agent's persistent-shell session name: `…-agent-<id>-shell-<sid>[-<name>]`.
# The agent's own process session (`…-agent-<id>`, no `-shell-`) does not match.
_AGENT_SHELL_RE = re.compile(r"-agent-(\d+)-shell-(\d+)(?:-(.+))?$")


def agent_shell_sessions(agent_id: int) -> list[ShellInfo]:
    """This host's live persistent-shell sessions for one agent, newest id last.

    Reuses the same session enumeration as the cluster status snapshot
    (`_collect_sessions`, already filtered to this cluster's prefix), keeping
    only `…-agent-<agent_id>-shell-<sid>[-<name>]` sessions — the agent's
    explicitly created shells and its watchers. The agent's own process session
    has no `-shell-` segment and is excluded.

    **Host scope: this machine's session backends only.** The gateway's inspector
    and shell-monitor endpoints call this for agents that run on THIS host and
    dispatch `shell_probe` / `shell_capture` ops to the agent's own machine
    otherwise — a remote runner's shells never appear in a local probe.
    """
    sessions, *_ = _collect_sessions()
    shells: list[ShellInfo] = []
    for s in sessions:
        m = _AGENT_SHELL_RE.search(s.name)
        if m is None or int(m.group(1)) != agent_id:
            continue
        shells.append(
            ShellInfo(
                id=int(m.group(2)),
                name=m.group(3),
                created_at=s.created_at,
                uptime_seconds=s.uptime_seconds,
            )
        )
    shells.sort(key=lambda sh: sh.id)
    return shells


class ShellNotFoundError(LookupError):
    """No live shell with this session id exists on this host."""


def capture_shell(
    agent_id: int, session_id: int, lines: int = 200
) -> tuple[str, list[str], datetime | None, int]:
    """Capture the terminal tail of one of an agent's persistent shells.

    Resolves `session_id` against this host's live shell sessions for the agent
    (`agent_shell_sessions`), reconstructs the full session name (carrying the
    optional `-<name>` suffix), and captures the last `lines` lines through the
    shell session backend (per-session pty hosts on POSIX; the native supervisor on
    Windows — the backend's exact-match capture pins it to that one session,
    never a prefix neighbour `shell-3` vs `shell-30`, or its `-watcher`). Returns
    (full_name, captured_lines, created_at, uptime_seconds) — lines newline-split
    with the trailing newline stripped; created_at / uptime_seconds come from the
    resolved session record (the launch epoch + probe-time uptime).

    Host scope matches `agent_shell_sessions`: this host's session backend only.
    The gateway's shell-monitor endpoint runs this locally for agents on this
    host and dispatches the `shell_capture` op to the agent's own machine
    otherwise.

    Raises:
        ShellNotFoundError: no live shell with `session_id` on this host.
        RuntimeError: the capture failed (the session died between the
            probe and the capture).
    """
    shell = next((s for s in agent_shell_sessions(agent_id) if s.id == session_id), None)
    if shell is None:
        raise ShellNotFoundError(f"agent {agent_id} has no live shell {session_id} on this host")

    full_name = shared.cluster.session_name(f"agent-{agent_id}-shell-{session_id}") + (
        f"-{shell.name}" if shell.name else ""
    )
    from shared.session_backend import get_shell_backend

    try:
        captured = get_shell_backend().capture_pane(full_name, lines)
    except Exception as exc:
        raise RuntimeError(f"session capture on {full_name!r} failed: {exc}") from exc
    captured_lines = captured.splitlines()
    # Trim blank padding from both ends of the tail. A cursor-addressed TUI
    # (claude/codex-class CLIs) redraws via escape sequences, and each
    # full-screen redraw scrolls cleared rows into the pyte scrollback — so a
    # capture can open with dozens of blank rows the shell-monitor page
    # renders as a huge blank region above the real output. Trailing blank
    # screen rows below the last line of a short session do the mirror-image
    # damage: the bottom-anchored pane scrolls the real output above the fold.
    # Blank padding at the extremes of a capture is never meaningful output;
    # interleaved blanks stay.
    while captured_lines and not captured_lines[0].strip():
        captured_lines.pop(0)
    while captured_lines and not captured_lines[-1].strip():
        captured_lines.pop()
    return full_name, captured_lines, shell.created_at, shell.uptime_seconds


def kill_shell(agent_id: int, session_id: int) -> tuple[str, bool, str | None]:
    """Kill one host-local persistent shell, or report that it is already absent.

    Returns ``(mode, interrupted, name)``: ``mode`` is ``"killed"`` or
    ``"absent"``; ``interrupted`` is True when the killed session carried live
    processes (a running foreground or background job) at kill time — the TTL
    reaper uses it to decide whether the reclamation deserves a notice to the
    owner (an empty shell's reaping is silent); ``name`` is the shell's
    optional display name. The verdict comes from the same backend call that
    kills (``kill_session_with_verdict``), so a job starting between a
    separate idle probe and the kill cannot be missed; a backend without
    verdict support reports interrupted=True (fail-open: a session that
    cannot be proven idle may well be running work)."""
    shell = next((s for s in agent_shell_sessions(agent_id) if s.id == session_id), None)
    if shell is None:
        return "absent", False, None
    full_name = shared.cluster.session_name(f"agent-{agent_id}-shell-{session_id}") + (
        f"-{shell.name}" if shell.name else ""
    )
    from shared.session_backend import get_shell_backend

    backend = get_shell_backend()
    try:
        ok, _mode, interrupted = backend.kill_session_with_verdict(full_name)
    except NotImplementedError:
        interrupted = True  # cannot inspect — assume the worst
        ok, _mode = backend.kill_session(full_name)
    if not ok:
        raise RuntimeError(f"failed to kill session {full_name!r}")
    return "killed", interrupted, shell.name


def _collect_sessions() -> tuple[list[SessionInfo], int, int]:
    """Enumerate this host's live sessions from the session backends, and return
    (sessions, shell_count, total).

    The service/daemon sessions come from `get_backend()` (native supervisor on
    POSIX, winproc on Windows) and the agents' persistent shells / watchers from
    `get_shell_backend()` (per-session pty hosts on POSIX) — the same two namespaces
    `ava start` / the healthchecks write into. Only sessions matching the
    current cluster prefix (`ava-*`) are kept — dev-worktree clusters and bare
    non-ava sessions are excluded. Agent processes are not sessions (they are
    pid records, tracked separately), so bare `ava-agent-<id>` sessions are
    filtered out here — this covers the daemons + the agents' persistent
    shells. A backend that is down degrades to empty data.
    """
    from shared.session_backend import get_backend, get_shell_backend

    rows: dict[str, SessionInfo] = {}
    now = datetime.now().astimezone(cluster_tz())
    for backend in (get_backend(), get_shell_backend()):
        try:
            names = backend.list_sessions(_CLUSTER_SESSION_PREFIX)
        except Exception:
            # fail-fast-ok: a down backend means no sessions to show, and the
            # status snapshot must stay readable while it is down.
            _log.warning("session enumeration failed on %s", type(backend).__name__, exc_info=True)
            continue
        # Batch timestamp read: the PTY backend's per-session path costs one
        # CLI process per session (~150 ms each) and a snapshot fans out over
        # ALL of them serially — 28 sessions measured ~4.5 s, blowing past the
        # roster's 3 s probe timeout (2026-08-12, misreported machine-1 offline).
        epochs = backend.session_started_ats(names)
        for name in names:
            if _AGENT_PROCESS_RE.match(name):
                continue  # agent processes are pid records, not sessions
            created: datetime | None = None
            epoch = epochs.get(name)
            if epoch is not None:
                try:
                    created = datetime.fromtimestamp(epoch).astimezone(cluster_tz())
                except (OSError, OverflowError, ValueError):
                    created = None
            uptime = int((now - created).total_seconds()) if created else 0
            rows[name] = SessionInfo(name=name, created_at=created, uptime_seconds=uptime)
    sessions = sorted(rows.values(), key=lambda s: s.name)
    return sessions, _count_agent_shells(sessions), len(sessions)


def _read_deploy_snapshot(
    pool: Any | None,
) -> tuple[
    shared.host_deploy_state.HostDeployState | None,
    shared.cluster_lock.DeployLease | None,
    int,
]:
    """Read deploy state and local agent count through one snapshot-local connection."""
    try:
        connection = (
            shared.db.connect(autocommit=True)
            if pool is None
            else pool.connection(timeout=_POOL_BORROW_TIMEOUT_S)
        )
        with connection as conn:
            state = shared.host_deploy_state.read(conn=conn)
            lease = shared.cluster_lock.read_update_lease(conn=conn)
            agent_count = _count_local_agents(conn) if is_agent_runner() else 0
        return state, lease, agent_count
    except Exception:  # fail-fast-ok: status degrades when the central DB is unavailable
        # Deploy state and agent count share one bounded connection. During a
        # data-plane outage the snapshot remains readable with no deploy claim
        # and the existing zero-count default.
        _log.warning("deploy-state snapshot read failed; using degraded status", exc_info=True)
        return None, None, 0


def _read_resource_sample() -> ResourceSample | None:
    """One live resource sample, degraded to None on any psutil failure."""
    try:
        from shared.resource_sample import resource_sample

        return resource_sample()
    except Exception:  # fail-fast-ok: psutil may not be installed; degrade gracefully
        _log.warning("resource_sample failed (psutil missing?)", exc_info=True)
        return None


def status_snapshot(pool: Any | None = None) -> ClusterStatus:
    """Assemble this host's cluster state — used by `/api/cluster/status`.

    When setup is missing, shared/machine.py's machine_name /
    machine_role raise specific exceptions; this function passes them
    through and FastAPI surfaces as default 500 (admin endpoint, not
    consumed by SDK).
    """
    from shared import process_sha as _process_sha
    from shared.cluster_drift import prod_source_head_sha
    from shared.config import settings

    agent_host_alive = (
        _check_pidfile(str(settings.services.agent_host_pidfile))[0] if is_agent_runner() else None
    )
    # One watchdog per capability now; `watchdog_online` (single bool for the
    # frontend dot) means "every watchdog this host should run is alive". A
    # single-box host requires BOTH; a split unit requires only its own.
    watchdog_pidfiles: list[str] = []
    if is_gateway():
        watchdog_pidfiles.append(str(settings.services.gateway_watchdog_pidfile))
    if is_agent_runner():
        watchdog_pidfiles.append(str(settings.services.agent_runner_watchdog_pidfile))
    watchdog_alive = (
        all(_check_pidfile(p)[0] for p in watchdog_pidfiles) if watchdog_pidfiles else False
    )
    sessions, shell_count, session_total = _collect_sessions()
    # The producer is typed (AgentSessionGroup); ClusterStatus.agent_groups stays
    # an open dict list so the frontend-facing status schema (and its generated TS
    # types) is unchanged — serialize the models to JSON dicts at the boundary.
    agent_groups = [g.model_dump(mode="json") for g in _group_agent_sessions(sessions)]
    # The live one-shot resource sample blocks for its CPU interval. Run it in
    # parallel with the central-DB reads so snapshot latency pays the slower
    # of those independent operations, not their sum.
    with ThreadPoolExecutor(max_workers=1) as executor:
        resource_future = executor.submit(_read_resource_sample)
        state, lease, agent_count = _read_deploy_snapshot(pool)
        resource = resource_future.result()
    return ClusterStatus(
        machine_name=machine_name(),
        serve_gateway=is_gateway(),
        serve_agent_runner=is_agent_runner(),
        serve_observability_station=is_observability_station(),
        paused=cluster_pause.is_paused(state),
        current_orchestration=cluster_session.current_orchestration(state, lease),
        last_updater_outcome=last_updater_outcome(state),
        head_sha=prod_source_head_sha(),
        running_sha=_process_sha.get(),
        shell_count=shell_count,
        agent_host_online=agent_host_alive,
        watchdog_online=watchdog_alive,
        agent_count=agent_count,
        session_count=session_total,
        agent_groups=agent_groups,
        resource=resource,
    )
