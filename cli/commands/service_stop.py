"""Stop a drained unit's recorded services and persistent terminals.

The caller owns the maintenance journal and admission fence. These functions
prove only local recorded process identities; they do not prove remote drain or
stop OS-managed extras. Ordinary stops never escalate to force. Persistent
terminals close at their own boundary: `close_terminals` for `ava stop`, and
`close_release_terminals` at a release or a PITR activation, where no terminal
survives (decisions/2026-09-27-fleet-release-and-cutover-policies.md item 2;
decisions/2026-09-27-unit-join-pitr-closure-fleet-policy.md item 2). Both take a
session's whole membership as `shared.sessions.pty.session_tree` defines it —
the recorded shell, its descendants and its POSIX session — and KILL reaches
only identities captured from the terminal records' shells and hosts.
"""

from __future__ import annotations

import contextlib
import math
import signal
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

import psutil

from cli.commands._maintenance_stop_report import (
    StopIncompleteError,
    StopSurvivor,
    SurvivorInventory,
    capture_survivor,
    live_identities,
    occupied_groups,
)
from ops import pty_close_notices
from shared.machine import machine_name
from shared.native_process.ownership import OwnedProcess, capture_tree, retain_processes
from shared.paths import run_dir
from shared.session_backend import get_shell_backend
from shared.session_record import SessionRecord
from shared.sessions.pty import host_identity, host_starttime, session_tree

# How often the completed-work wait re-reads the terminal trees.
_WORK_POLL_S = 0.5


def deadline_after(timeout: float) -> float:
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("maintenance stop timeout must be finite and positive")
    return time.monotonic() + timeout


def remaining(deadline: float) -> float:
    value = deadline - time.monotonic()
    if value <= 0:
        raise TimeoutError("maintenance kept its hold; stop deadline expired")
    return value


def wait_for_exit(
    tracked: set[OwnedProcess],
    deadline: float,
    *,
    groups: tuple[int, ...] = (),
    escalate: Callable[[set[OwnedProcess]], None] | None = None,
) -> None:
    """Wait for `tracked` identities and `groups` to empty, within `deadline`.

    `escalate`, when given, is called once per iteration with the current
    living identities, AFTER the descendant re-capture: it may deliver further
    graceful signals to confirmed-owned identities (issue #2123 — a leader that
    exits without closing its children must not stall the stop until the
    deadline; the caller's escalator owns the ownership validation). Never
    escalates to SIGKILL and never certifies success while anything is alive.
    """
    while True:
        living = {identity for identity in tracked if identity.live()}
        occupied = occupied_groups(groups)
        if not living and not occupied:
            return
        for identity in living:
            retain_processes(tracked, capture_tree(identity))
        if escalate is not None:
            escalate(living)
        try:
            budget = remaining(deadline)
        except TimeoutError:
            raise TimeoutError(
                f"maintenance kept its hold; processes did not exit: "
                f"{sorted(identity.pid for identity in living)}; occupied process groups: {occupied}"
            ) from None
        time.sleep(min(0.05, budget))


def require_no_terminals() -> None:
    terminals = live_terminals()
    if terminals:
        raise RuntimeError(
            "persistent terminals/schedules require their own completed-work boundary; "
            f"maintenance will not kill or replay them: {terminals}"
        )


def live_terminals() -> list[str]:
    """Every session whose recorded shell or PTY host birth is live, or that is listed."""
    # A PTY host can remain alive after its shell exits. The ordinary listing
    # intentionally omits that retained record, so inspect both recorded births.
    terminals: list[str] = []
    for path in (run_dir() / "pty").glob("*.json"):
        record = SessionRecord.read(path)
        if record is None:
            raise RuntimeError(f"cannot verify terminal record: {path.stem}")
        identities = [OwnedProcess(record.pid, record.create_time, record.starttime)]
        host = host_identity(path)
        if host is not None:
            identities.append(OwnedProcess(host[0], host[1], host_starttime(path)))
        if any(identity.live() for identity in identities):
            terminals.append(path.stem)
    backend = get_shell_backend()
    terminals.extend(backend.list_sessions())
    return sorted(set(terminals))


@dataclass(frozen=True)
class TerminalInventory:
    """Every live recorded terminal, captured before any signal.

    `jobs` maps each captured member of a shell's session beyond the shell (its
    descendants and its POSIX session, `session_tree.session_members`) to that
    session; `hosts` holds each session's recorded PTY host. A session is busy
    when it has a job.
    """

    shells: dict[str, OwnedProcess]
    jobs: dict[OwnedProcess, str]
    hosts: dict[str, OwnedProcess]

    @property
    def busy(self) -> dict[str, OwnedProcess]:
        return {name: self.shells[name] for name in sorted(set(self.jobs.values()))}

    def processes(self) -> set[OwnedProcess]:
        return set(self.shells.values()) | set(self.jobs)

    def session_of(self, identity: OwnedProcess) -> str | None:
        for recorded in (self.shells, self.hosts):
            for name, process in recorded.items():
                if process == identity:
                    return name
        return self.jobs.get(identity)

    def survivors(self, identities: list[OwnedProcess]) -> list[StopSurvivor]:
        """Each identity's report entry, attributed to its recorded session."""

        def role(identity: OwnedProcess) -> str:
            if identity in self.shells.values():
                return "terminal"
            return "pty-host" if identity in self.hosts.values() else "job"

        return [
            capture_survivor(identity, service=self.session_of(identity), role=role(identity))
            for identity in identities
        ]


def capture_terminals() -> TerminalInventory:
    """Capture each live recorded session's shell, jobs and PTY host; signal nothing.

    A job is any live member of the shell's session: `cmd &` in its own
    process group and a double-forked orphan that left the shell's tree count
    the same as a foreground child.
    """
    shells: dict[str, OwnedProcess] = {}
    jobs: dict[OwnedProcess, str] = {}
    hosts: dict[str, OwnedProcess] = {}
    for name in get_shell_backend().list_sessions():
        path = run_dir() / "pty" / f"{name}.json"
        record = SessionRecord.read(path)
        if record is None:
            continue
        shell = OwnedProcess(record.pid, record.create_time, record.starttime)
        if not shell.live():
            continue
        shells[name] = shell
        host = host_identity(path)
        if host is not None:
            hosts[name] = OwnedProcess(host[0], host[1], host_starttime(path))
        for identity in session_tree.session_members(shell):
            if identity != shell:
                jobs[identity] = name
    return TerminalInventory(shells, jobs, hosts)


def _cancel_terminals(inventory: TerminalInventory) -> None:
    """The graceful close: HUP each shell, then TERM each captured job.

    The spawners stop FIRST: an interactive shell's own SIGHUP makes bash exit
    (re-sending HUP to its jobs), so a loop that restarts its job cannot keep
    producing new descendants during the wait — the 2026-09-09 field evidence
    showed a restart loop outliving every interrupt aimed at its current job
    (#2045). Jobs get their graceful SIGTERM right after.
    """
    for shell in inventory.shells.values():
        if shell.live():
            with contextlib.suppress(psutil.NoSuchProcess, psutil.ZombieProcess):
                shell.send_signal(signal.SIGHUP)
    for job in inventory.jobs:
        if job.live():
            with contextlib.suppress(psutil.NoSuchProcess, psutil.ZombieProcess):
                job.send_signal(signal.SIGTERM)


def _await_closed(
    inventory: TerminalInventory, tracked: set[OwnedProcess], deadline: float
) -> None:
    """Wait for every tracked identity to exit; never force, report each survivor."""
    try:
        wait_for_exit(tracked, deadline)
    except TimeoutError as exc:
        # A shell that HUP'd out may have dropped its record while a
        # signal-ignoring job survives as an orphan — report the owning
        # session by name and each survivor's identity (issue #2162) so the
        # operator can find and judge the exact process.
        live = live_identities(tracked)
        survivors = inventory.survivors(live)
        surviving = sorted({name for name in map(inventory.session_of, live) if name})
        raise StopIncompleteError(
            f"terminal stop incomplete — {exc} surviving terminal processes "
            f"from sessions: {surviving}\n"
            f"{SurvivorInventory(survivors=survivors, groups=[]).render(stage='terminals')}",
            stage="terminals",
            survivors=[survivor.payload() for survivor in survivors],
        ) from exc


def _await_no_terminals(deadline: float) -> None:
    """The closure evidence: no recorded shell or PTY host birth remains live.

    Hosts finish naturally after their child exits. Their protocol deliberately
    ignores SIGTERM, so sending signals to every host process is not a stop API.
    """
    while True:
        try:
            require_no_terminals()
            return
        except RuntimeError:
            time.sleep(min(0.05, remaining(deadline)))


def _record_close_notices(
    busy: dict[str, OwnedProcess], operation: str, acquired_at: datetime, *, reason: str
) -> None:
    """Durably record one closure notice per busy session (issue #2044).

    An idle session, a timed-out stop, or a Windows unit records nothing. A
    write failure is loud but never fails the closure — retrying the whole stop
    would not restore the resources it closes.
    """
    for name, shell in busy.items():
        if shell.starttime is not None:
            birth = f"starttime:{shell.starttime}"
        else:
            birth = f"birth:{shell.birth!r}"
        try:
            pty_close_notices.record_close(
                machine=machine_name(),
                name=name,
                shell_pid=shell.pid,
                shell_birth=birth,
                operation=operation,
                acquired_at=acquired_at,
                reason=reason,
            )
        except Exception as exc:
            # The side-channel notice must never fail a closure; stay loud so
            # the gap is visible either way.
            print(
                f"closure notice for session {name!r} could not be recorded: "
                f"{type(exc).__name__}: {exc}",
                file=sys.stderr,
            )


def close_terminals(deadline: float, operation: str, acquired_at: datetime) -> None:
    """Close this unit's terminal jobs and shells without a kill escalation (`ava stop`).

    Busy sessions verified closed leave a durable closure notice for their
    owner agent (issue #2044): the gateway and ops server are already down by
    now, so the notice is delivered at the next ops-daemon startup.
    """
    if sys.platform == "win32":
        backend = get_shell_backend()
        for name in backend.list_sessions():
            ok, _mode = backend.kill_session(name, graceful=True, timeout=remaining(deadline))
            if not ok:
                raise RuntimeError(f"terminal {name!r} lacks a native Job closure receipt")
        return
    inventory = capture_terminals()
    _cancel_terminals(inventory)
    _await_closed(inventory, inventory.processes(), deadline)
    _await_no_terminals(deadline)
    _record_close_notices(
        inventory.busy, operation, acquired_at, reason=pty_close_notices.STOP_REASON
    )


def await_terminal_work(timeout: float) -> list[str]:
    """Observe until no recorded terminal carries a job, or `timeout` passes.

    The completed-work bound before a release closes its terminal writers: it
    signals nothing, so a job that finishes in time is never interrupted.
    Returns the sessions still busy at the bound.
    """
    _require_pty_custody()
    deadline = deadline_after(timeout)
    while True:
        busy = sorted(capture_terminals().busy)
        left = deadline - time.monotonic()
        if not busy or left <= 0:
            return busy
        time.sleep(min(_WORK_POLL_S, left))


def close_release_terminals(
    operation: str, acquired_at: datetime, *, grace_s: float, kill_s: float, reason: str
) -> TerminalInventory:
    """Close every persistent terminal at a release or PITR boundary; none survives it.

    Busy sessions' owner notices are recorded first, as the closure's intent,
    naming `reason` (`pty_close_notices.RELEASE_REASON` or `.PITR_REASON`). Then
    the ordinary graceful close, bounded by `grace_s`; whatever is still live
    after it dies by `_kill_terminals` (each session whole, then the recorded
    PTY hosts). Closure is the kernel observation, within `kill_s`, that every
    captured identity is gone and no recorded terminal remains live. A
    survivor, or a terminal born during closure, fails with the process
    inventory and leaves the boundary unresolved.
    """
    _require_pty_custody()
    inventory = capture_terminals()
    _record_close_notices(inventory.busy, operation, acquired_at, reason=reason)
    tracked = inventory.processes() | set(inventory.hosts.values())
    _cancel_terminals(inventory)
    graceful = True
    try:
        wait_for_exit(tracked, deadline_after(grace_s))
    except TimeoutError:
        graceful = False
    kill_deadline = deadline_after(kill_s)
    if not graceful:
        _kill_terminals(inventory, tracked, kill_deadline)
    try:
        wait_for_exit(tracked, kill_deadline)
    except TimeoutError as exc:
        survivors = inventory.survivors(live_identities(tracked))
        raise StopIncompleteError(
            f"release terminal closure incomplete — {exc}; survivors of SIGKILL "
            "to their captured births:\n" + "\n".join(entry.render() for entry in survivors),
            stage="release-terminals",
            survivors=[entry.payload() for entry in survivors],
        ) from exc
    born = live_terminals()
    if born:
        raise RuntimeError(f"terminals live after release closure: {born}")
    return inventory


def _kill_terminals(
    inventory: TerminalInventory, tracked: set[OwnedProcess], deadline: float
) -> None:
    """SIGKILL what the graceful close left, each session whole.

    Every session dies through `session_tree.kill_session_tree`: frozen parents
    first, killed children first with the shell last, rooted at the shell and
    at each job captured before the cancel, so a job whose shell already exited
    is still taken with its descendants. Every member that kill captured joins
    `tracked` for the closure evidence. Whatever else is tracked and still live
    — a recorded PTY host, a descendant re-captured during the grace wait —
    then gets SIGKILL to its captured birth.
    """
    for name, shell in inventory.shells.items():
        jobs = [job for job, owner in inventory.jobs.items() if owner == name]
        result = session_tree.kill_session_tree(
            shell, also=jobs, wait_s=max(0.0, deadline - time.monotonic())
        )
        tracked.update(result.killed, result.survivors)
    for identity in sorted(tracked, key=lambda identity: identity.pid):
        identity.send_signal(signal.SIGKILL)


def _require_pty_custody() -> None:
    if sys.platform == "win32":
        raise RuntimeError("release terminal closure requires POSIX PTY custody")


def stop_services(
    timeout: float, *, keep_terminals: bool = False, selected: frozenset[str] | None = None
) -> list[str]:
    """Ask the sole root owner to stop drained services without force escalation."""
    from cli.commands.root_driver import _root_tree_selection, _stop_root_service_tree

    deadline = deadline_after(timeout)
    if not keep_terminals:
        require_no_terminals()
    names = _root_tree_selection()
    selected_names = sorted(names if selected is None else names.keys() & selected)
    if selected is not None and not selected_names:
        return []
    preserve = frozenset(unit for name, unit in names.items() if name not in selected_names)
    _stop_root_service_tree(
        preserve=preserve,
        timeout_s=remaining(deadline),
        force=False,
        selected=None if selected is None else frozenset(names[name] for name in selected_names),
    )
    if not keep_terminals:
        require_no_terminals()
    return selected_names


def stop_data_plane(timeout: float, *, save: bool = True) -> list[str]:
    """Stop this home's native data plane; never stop a remote-managed plane."""
    from cli.commands.data_plane.maintenance_stop import stop

    return stop(timeout, save=save)
