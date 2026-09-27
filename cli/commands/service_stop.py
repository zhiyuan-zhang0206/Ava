"""Stop a drained unit's recorded services and persistent terminals.

The caller owns the maintenance journal and admission fence. These functions
prove only local recorded process identities; they do not prove remote drain or
stop OS-managed extras. Service stops never escalate to force.

Persistent terminals have one closure (`_close`): capture each shell's whole
session as `shared.sessions.pty.session_tree` defines it — the recorded shell,
its descendants and its POSIX session — then HUP the shells and TERM the rest,
wait a bounded grace, and SIGKILL what is left, each session whole. Its callers
differ only in the grace, the SIGKILL leg's bound, the report's stage and when
the owner's notice is recorded: `close_terminals` at `ava stop`
(decisions/2026-09-28-stop-escalates-to-sigkill.md), `close_release_terminals`
at a release or a PITR activation
(decisions/2026-09-27-fleet-release-and-cutover-policies.md item 2;
decisions/2026-09-27-unit-join-pitr-closure-fleet-policy.md item 2). KILL
reaches only identities captured from the terminal records' shells and hosts.
"""

from __future__ import annotations

import math
import signal
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from cli.commands._maintenance_stop_report import (
    StopIncompleteError,
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

# How long a normal stop's terminal closure waits between its HUP/TERM and the
# SIGKILL of whatever is left (decisions/2026-09-28-stop-escalates-to-sigkill.md):
# a job that handles TERM gets this long to clean up. The stop's own deadline
# caps it as well. A release or PITR activation passes its own grace.
_TERMINAL_STOP_GRACE_S = 10.0

# The SIGKILL leg's own bound at a normal stop: each wait inside a session kill,
# the wait for the killed sessions' hosts to end, and the closure evidence.
# Every wait ends as soon as its processes are gone. The leg runs even when the
# grace spent the rest of the stop deadline — a stop that reached its terminal
# phase closes its terminals — so a stop can overrun its deadline by this
# bounded leg.
_TERMINAL_KILL_WAIT_S = 3.0


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


@dataclass
class _Terminal:
    """One persistent shell a closure takes, with everything captured as its session's.

    `members` is the session's membership as `session_tree.session_members`
    defines it — the shell, its descendants and every process in its POSIX
    session (`cmd &` in its own group, a double-forked orphan) — each pinned by
    birth before any signal; a live member's new descendants join it while the
    closure waits. `host` is the recorded PTY host. Anything beyond the shell
    at capture is running work, so the session is busy.
    """

    name: str
    shell: OwnedProcess
    members: set[OwnedProcess]
    host: OwnedProcess | None
    busy: bool

    def role(self, identity: OwnedProcess) -> str:
        if identity == self.shell:
            return "terminal"
        return "pty-host" if identity == self.host else "job"


@dataclass(frozen=True)
class TerminalInventory:
    """Every live recorded terminal, captured before any signal."""

    terminals: tuple[_Terminal, ...]

    @property
    def shells(self) -> dict[str, OwnedProcess]:
        return {terminal.name: terminal.shell for terminal in self.terminals}

    @property
    def busy(self) -> dict[str, OwnedProcess]:
        return {terminal.name: terminal.shell for terminal in self.terminals if terminal.busy}


def capture_terminals() -> TerminalInventory:
    """Capture each live recorded session's membership and PTY host; signal nothing."""
    return TerminalInventory(tuple(_capture_terminals(get_shell_backend().list_sessions())))


def _capture_terminals(names: list[str]) -> list[_Terminal]:
    """Each live shell's session membership and PTY host, captured before any signal."""
    terminals: list[_Terminal] = []
    for name in names:
        path = run_dir() / "pty" / f"{name}.json"
        record = SessionRecord.read(path)
        if record is None:
            continue
        shell = OwnedProcess(record.pid, record.create_time, record.starttime)
        members = session_tree.session_members(shell)
        if not members:
            continue  # the shell is no longer the recorded, live process
        recorded_host = host_identity(path)
        host = None
        if recorded_host is not None:
            host = OwnedProcess(recorded_host[0], recorded_host[1], host_starttime(path))
        terminals.append(_Terminal(name, shell, set(members), host, busy=len(members) > 1))
    return terminals


def _hang_up(terminals: list[_Terminal]) -> None:
    """SIGHUP every shell, then SIGTERM every other captured member.

    The shells go first: an interactive shell's own SIGHUP makes bash exit
    (re-sending HUP to its jobs), so a loop that restarts its job cannot keep
    producing new descendants during the grace — the 2026-09-09 field evidence
    showed a restart loop outliving every interrupt aimed at its current job
    (#2045).
    """
    session_tree.terminate([terminal.shell for terminal in terminals], signal.SIGHUP)
    for terminal in terminals:
        session_tree.terminate(terminal.members - {terminal.shell})


def _await_members(terminals: list[_Terminal], until: float) -> bool:
    """Wait for every captured member to exit; False when `until` passes first.

    A live member's new descendants join its session's capture on every poll,
    so a child forked during the grace dies with the rest.
    """
    while True:
        live = [(t, identity) for t in terminals for identity in list(t.members) if identity.live()]
        if not live:
            return True
        for terminal, identity in live:
            terminal.members |= capture_tree(identity)
        left = until - time.monotonic()
        if left <= 0:
            return False
        time.sleep(min(0.05, left))


def _kill_leftovers(
    terminals: list[_Terminal], wait_s: float
) -> list[tuple[_Terminal, OwnedProcess]]:
    """SIGKILL each session's remaining membership; return what outlived it.

    Every session dies through `session_tree.kill_session_tree`: frozen, killed
    children first with the shell last, rooted at the shell and at every member
    captured, so a job whose shell already exited is still taken with its
    descendants.
    """
    survivors: list[tuple[_Terminal, OwnedProcess]] = []
    for terminal in terminals:
        if not any(identity.live() for identity in terminal.members):
            continue
        result = session_tree.kill_session_tree(
            terminal.shell, also=terminal.members, wait_s=wait_s
        )
        survivors += [(terminal, identity) for identity in result.survivors]
    return survivors


def _end_hosts(terminals: list[_Terminal], wait_s: float) -> list[tuple[_Terminal, OwnedProcess]]:
    """Let each PTY host end once its session is gone; SIGKILL one that does not.

    A host exits on its own the moment its shell is gone, unlinking its record
    first; it ignores HUP and TERM by protocol, so a signal is no stop API for a
    healthy one. A host still running `wait_s` later is wedged, and a closure
    leaves no captured birth running: it gets SIGKILL. Returns the hosts that
    outlived that too.
    """
    hosts = {terminal.host: terminal for terminal in terminals if terminal.host is not None}
    if not _await_gone(list(hosts), wait_s):
        for host in live_identities(hosts):
            host.send_signal(signal.SIGKILL)
        _await_gone(list(hosts), wait_s)
    return [(hosts[host], host) for host in live_identities(hosts)]


def _await_gone(identities: list[OwnedProcess], wait_s: float) -> bool:
    """Poll until none of `identities` is live; False when `wait_s` passes first."""
    until = time.monotonic() + wait_s
    while live_identities(identities):
        left = until - time.monotonic()
        if left <= 0:
            return False
        time.sleep(min(0.05, left))
    return True


def _terminals_incomplete(
    survivors: list[tuple[_Terminal, OwnedProcess]], stage: str
) -> StopIncompleteError:
    """The report for processes that outlived their SIGKILL (issue #2162's inventory).

    Each survivor names its owning session and its identity so the operator
    can find and judge the exact process — typically another user's (a root
    `sudo`), which this closure may not signal.
    """
    owners = {identity: terminal for terminal, identity in survivors}
    live = live_identities(owners)
    report = [
        capture_survivor(
            identity, service=owners[identity].name, role=owners[identity].role(identity)
        )
        for identity in live
    ]
    surviving = sorted({owners[identity].name for identity in live})
    return StopIncompleteError(
        f"terminal closure incomplete — processes outlived their SIGKILL: "
        f"{[identity.pid for identity in live]} from sessions: {surviving}\n"
        f"{SurvivorInventory(survivors=report, groups=[]).render(stage=stage)}",
        stage=stage,
        survivors=[survivor.payload() for survivor in report],
    )


def _close(terminals: list[_Terminal], *, grace_until: float, kill_s: float, stage: str) -> None:
    """The one terminal closure: hang up, a bounded grace, SIGKILL, then the evidence.

    Each shell's whole session was captured before this first signal
    (`_capture_terminals`). The shells get HUP and every other member TERM
    (`_hang_up`); whatever is still alive at `grace_until` is SIGKILLed with its
    session (`_kill_leftovers`), and each PTY host ends after its session
    (`_end_hosts`). The SIGKILL leg runs even when `grace_until` has already
    passed, each of its waits bounded by `kill_s`. A captured process that
    outlives its SIGKILL fails the closure with its identity
    (`StopIncompleteError` at `stage`); so does a terminal still live after
    it (`_await_no_terminals`).
    """
    _hang_up(terminals)
    graceful = _await_members(terminals, grace_until)
    survivors = [] if graceful else _kill_leftovers(terminals, kill_s)
    survivors += _end_hosts(terminals, kill_s)
    if live_identities(identity for _terminal, identity in survivors):
        raise _terminals_incomplete(survivors, stage)
    _await_no_terminals(time.monotonic() + kill_s)


def _await_no_terminals(until: float) -> None:
    """The closure evidence: no recorded shell or PTY host birth is live; raise at `until`.

    A host clears its record the moment its shell is gone, so this only waits
    out a host still tearing down; a terminal born during the closure fails it.
    """
    while True:
        left = live_terminals()
        if not left:
            return
        if time.monotonic() >= until:
            raise RuntimeError(f"terminals live after closure: {left}")
        time.sleep(0.05)


def _record_close_notices(
    busy: dict[str, OwnedProcess], operation: str, acquired_at: datetime, *, reason: str
) -> None:
    """Durably record one closure notice per busy session (issue #2044).

    An idle session, a failed stop, or a Windows unit records nothing. A write
    failure is loud but never fails the closure — retrying the whole stop would
    not restore the resources it closes.
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
    """Close this unit's terminals at `ava stop`: HUP/TERM, a bounded grace, then SIGKILL.

    The grace is `_TERMINAL_STOP_GRACE_S`, capped by the stop's `deadline`;
    the SIGKILL leg is bounded by `_TERMINAL_KILL_WAIT_S` and runs even when
    the grace spent the rest of the deadline — a stop that reached its
    terminal phase closes its terminals
    (decisions/2026-09-28-stop-escalates-to-sigkill.md). A process that
    outlives its SIGKILL fails the stop, which keeps its maintenance hold.

    Busy sessions verified closed — a job the SIGKILL cut short included —
    leave a durable closure notice for their owner agent (issue #2044): the
    gateway and ops server are already down by now, so the notice is delivered
    at the next ops-daemon startup.
    """
    if sys.platform == "win32":
        backend = get_shell_backend()
        for name in backend.list_sessions():
            ok, _mode = backend.kill_session(name, graceful=True, timeout=remaining(deadline))
            if not ok:
                raise RuntimeError(f"terminal {name!r} lacks a native Job closure receipt")
        return
    inventory = capture_terminals()
    _close(
        list(inventory.terminals),
        grace_until=min(deadline, time.monotonic() + _TERMINAL_STOP_GRACE_S),
        kill_s=_TERMINAL_KILL_WAIT_S,
        stage="terminals",
    )
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

    The same closure as `close_terminals`, with the boundary's own bounds:
    `grace_s` for the hang-up and `kill_s` for each wait of the SIGKILL leg.
    Busy sessions' owner notices are recorded first, as the closure's intent,
    naming `reason` (`pty_close_notices.RELEASE_REASON` or `.PITR_REASON`): the
    boundary cannot complete with the session alive, so no retry or crash may
    lose the notice. A survivor, or a terminal born during closure, fails with
    the process inventory and leaves the boundary unresolved.
    """
    _require_pty_custody()
    inventory = capture_terminals()
    _record_close_notices(inventory.busy, operation, acquired_at, reason=reason)
    _close(
        list(inventory.terminals),
        grace_until=time.monotonic() + grace_s,
        kill_s=kill_s,
        stage="release-terminals",
    )
    return inventory


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
