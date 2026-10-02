"""Stop a drained unit's recorded services and persistent terminals.

The caller owns the maintenance journal and admission fence. These functions
prove only local recorded process identities; they do not prove remote drain or
stop OS-managed extras. Service stops never escalate to force.

Persistent terminals have one closure (`_close`): capture each shell's whole
session as `base.sessions.pty.session_tree` defines it — the recorded shell,
its descendants and its POSIX session — then HUP the shells and TERM the rest,
wait a bounded grace, and SIGKILL what is left, each session whole. Every busy
session whose shell it verified gone gets its owner's notice, naming what of it
outlived the SIGKILL. `close_terminals` runs it at `ava stop`
(decisions/2026-09-28-stop-escalates-to-sigkill.md). KILL reaches only
identities captured from the terminal records' shells and hosts.
"""

from __future__ import annotations

import math
import signal
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

import psutil

from base import telemetry
from base.cluster import postgres as owned_postgres
from base.cluster.machine import machine_name
from base.native_process.ownership import OwnedProcess, capture_tree, retain_processes
from base.paths import run_dir
from base.sessions.backend import get_shell_backend
from base.sessions.pty import host_identity, host_starttime, session_tree
from base.sessions.record import SessionRecord
from cli.commands.lifecycle._maintenance_stop_report import (
    StopIncompleteError,
    SurvivorInventory,
    capture_survivor,
    live_identities,
    occupied_groups,
)
from ops import pty_close_notices

# How long a normal stop's terminal closure waits between its HUP/TERM and the
# SIGKILL of whatever is left (decisions/2026-09-28-stop-escalates-to-sigkill.md):
# a job that handles TERM gets this long to clean up. The stop's own deadline
# caps it as well.
_TERMINAL_STOP_GRACE_S = 10.0

# The SIGKILL leg's own bound at a normal stop: each wait inside a session kill,
# the wait for the killed sessions' hosts to end, and the least the closure
# evidence waits (it also gets the rest of the stop deadline).
# Every wait ends as soon as its processes are gone. The leg runs even when the
# grace spent the rest of the stop deadline — a stop that reached its terminal
# phase closes its terminals — so a stop can overrun its deadline by this
# bounded leg.
_TERMINAL_KILL_WAIT_S = 3.0

# The data-plane stop's escalation legs (`cli/commands/data_plane/maintenance_stop.py`)
# use the same two bounds under their own names: how long Postgres' immediate shutdown
# is given to finish, and how long the SIGKILL of what outlives it is waited for.
PROCESS_CLEANUP_WAIT_S = _TERMINAL_STOP_GRACE_S
PROCESS_KILL_WAIT_S = _TERMINAL_KILL_WAIT_S


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
    """Refuse maintenance while any terminal is present (`live_terminals`)."""
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

    `capture` is the session's membership as `session_tree.capture_session`
    takes it — the shell, its descendants and every process in its POSIX
    session (`cmd &` in its own group, a double-forked orphan) — each pinned by
    birth before any signal, kept current while the closure waits
    (`session_tree.refresh`). `host` is the recorded PTY host. Anything beyond
    the shell at capture is running work, so the session is busy.
    """

    name: str
    capture: session_tree.SessionCapture
    host: OwnedProcess | None
    busy: bool

    @property
    def shell(self) -> OwnedProcess:
        return self.capture.leader

    def role(self, identity: OwnedProcess) -> str:
        if identity == self.shell:
            return "terminal"
        return "pty-host" if identity == self.host else "job"


@dataclass(frozen=True)
class TerminalInventory:
    """Every live recorded terminal, captured before any signal."""

    terminals: tuple[_Terminal, ...]


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
        capture = session_tree.capture_session(shell)
        if not capture.members:
            continue  # the shell is no longer the recorded, live process
        recorded_host = host_identity(path)
        host = None
        if recorded_host is not None:
            host = OwnedProcess(recorded_host[0], recorded_host[1], host_starttime(path))
        terminals.append(_Terminal(name, capture, host, busy=len(capture.members) > 1))
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
        session_tree.terminate(set(terminal.capture.members) - {terminal.shell})


def _await_members(terminals: list[_Terminal], until: float) -> bool:
    """Wait for every captured member to exit; False when `until` passes first.

    Each poll folds each session's newcomers into its capture
    (`session_tree.refresh`): a live member's new descendants, and anything
    else in the session while its id is proven — a helper a job forked on TERM
    and orphaned before the next poll included. A member can fork while the
    poll that finds it gone is still scanning, so a quiet poll only counts
    once a second one, whose scan began after every member was gone, is quiet
    too.
    """
    captures = [terminal.capture for terminal in terminals]
    quiet = False
    while True:
        if not session_tree.refresh(captures):
            if quiet:
                return True
            quiet = True
            continue
        quiet = False
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
    descendants. One last refresh first, so every kill starts from its
    session's newest capture and proof; a session that can yield nothing more
    is skipped.
    """
    session_tree.refresh(terminal.capture for terminal in terminals)
    survivors: list[tuple[_Terminal, OwnedProcess]] = []
    for terminal in terminals:
        capture = terminal.capture
        if not capture.active:
            continue
        result = session_tree.kill_session_tree(
            capture.leader, also=capture.members, wait_s=wait_s, proven_at=capture.proven_at
        )
        survivors += [(terminal, identity) for identity in result.survivors]
    return survivors


def _end_hosts(terminals: list[_Terminal], wait_s: float) -> list[tuple[_Terminal, OwnedProcess]]:
    """Let each PTY host end once its session is gone; SIGKILL one that does not.

    A host exits on its own the moment its shell is gone, unlinking its record
    first; it ignores HUP and TERM by protocol, so a signal is no stop API for a
    healthy one. A host still running `wait_s` later is wedged, and a closure
    leaves no captured birth running: it gets SIGKILL. A host whose shell
    outlived its own SIGKILL is not wedged and stays with it: the closure fails
    on that shell, and a retry finds the session whole. Returns the hosts that
    outlived their SIGKILL.
    """
    hosts = {
        terminal.host: terminal
        for terminal in terminals
        if terminal.host is not None and not live_identities([terminal.shell])
    }
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
    inventory = SurvivorInventory(survivors=report, groups=[])
    return StopIncompleteError(
        f"terminal closure incomplete — processes outlived their SIGKILL: "
        f"{[identity.pid for identity in live]} from sessions: {surviving}\n"
        f"{inventory.render(stage=stage, killed=True)}",
        stage=stage,
        survivors=[survivor.payload() for survivor in report],
    )


@dataclass(frozen=True)
class _Notice:
    """What a closure's owner notices name (issue #2044): its operation, hold and reason."""

    operation: str
    acquired_at: datetime
    reason: str


# Each closed busy session's shell, and whatever of its session outlived the SIGKILL.
_Closed = dict[str, tuple[OwnedProcess, list[OwnedProcess]]]


def _close(
    terminals: list[_Terminal],
    notice: _Notice,
    *,
    direct_db: bool,
    grace_until: float,
    kill_s: float,
    stage: str,
    deadline: float,
) -> None:
    """The one terminal closure: hang up, a bounded grace, SIGKILL, then the evidence.

    Each shell's whole session was captured before this first signal
    (`_capture_terminals`). The shells get HUP and every other member TERM
    (`_hang_up`); whatever is still alive at `grace_until` is SIGKILLed with its
    session (`_kill_leftovers`), and each PTY host ends after its session
    (`_end_hosts`). The SIGKILL leg runs even when `grace_until` has already
    passed, each of its waits bounded by `kill_s`. Every busy session whose
    shell the kill left verified gone gets its owner notice first (`_closed`):
    a closed session's record is gone by any retry. Then a captured process
    that outlived its SIGKILL fails the closure with its identity
    (`StopIncompleteError` at `stage`); so does a terminal still live after it
    (`_await_no_terminals`), waited for until the caller's `deadline` and at
    least `kill_s`.
    """
    _hang_up(terminals)
    graceful = _await_members(terminals, grace_until)
    killed = [] if graceful else _kill_leftovers(terminals, kill_s)
    _record_close_notices(_closed(terminals, killed), notice, direct_db=direct_db)
    survivors = killed + _end_hosts(terminals, kill_s)
    if live_identities(identity for _terminal, identity in survivors):
        raise _terminals_incomplete(survivors, stage)
    settled = time.monotonic() + kill_s
    _await_no_terminals(max(deadline, settled), stage)


def _closed(terminals: list[_Terminal], killed: list[tuple[_Terminal, OwnedProcess]]) -> _Closed:
    """Every busy session whose shell is verified gone, with what of it outlived the SIGKILL.

    The shell is the session as its owner uses it: once it is gone the session
    cannot be used again, so it counts as closed — also when a process of it
    outlived the SIGKILL (the notice names it) and when another session keeps
    the closure incomplete (issue #2044's "notify only what actually closed",
    judged by the shell). A session whose shell still lives is not closed; a
    retry sees it again.
    """
    stuck = set(live_identities(identity for _terminal, identity in killed))
    left: dict[str, list[OwnedProcess]] = {}
    for terminal, identity in killed:
        if identity in stuck:
            left.setdefault(terminal.name, []).append(identity)
    return {
        terminal.name: (terminal.shell, left.get(terminal.name, []))
        for terminal in terminals
        if terminal.busy and not live_identities([terminal.shell])
    }


def _await_no_terminals(until: float, stage: str) -> None:
    """The closure evidence: no recorded shell or PTY host birth is live; raise at `until`.

    A host clears its record the moment its shell is gone, so this only waits
    out a host still tearing down; a terminal born during the closure fails it.
    """
    while True:
        left = live_terminals()
        if not left:
            return
        if time.monotonic() >= until:
            raise StopIncompleteError(
                f"terminal closure incomplete — terminals still present after the closure "
                f"ended every session it captured: {left}",
                stage=stage,
            )
        time.sleep(0.05)


def _named(identities: list[OwnedProcess]) -> list[tuple[int, str]]:
    """(pid, command name) of each process still running as its captured identity."""
    named: list[tuple[int, str]] = []
    for identity in sorted(identities, key=lambda identity: identity.pid):
        try:
            name = psutil.Process(identity.pid).name()
        except psutil.NoSuchProcess:
            continue
        except psutil.Error:
            name = "<unreadable>"
        if live_identities([identity]):  # the name was read from that process
            named.append((identity.pid, name))
    return named


def _record_close_notices(closed: _Closed, notice: _Notice, *, direct_db: bool) -> None:
    """Write one closure notice per closed busy session to the database (issue #2044).

    Each entry names the session's shell and the processes of it that outlived
    the SIGKILL. The write is one short connection, made here while the data
    plane is still up and closed before this returns (`pty_close_notices`). An
    idle session or one that is not an agent shell yields no notice, and no
    notice means no connection. A notice that cannot be written is loud on
    stderr, with the text its owner would have read, but never fails the
    closure — retrying the whole stop would not restore the resources it closes.
    """
    notices: list[pty_close_notices.ClosureNotice] = []
    for name, (shell, left) in closed.items():
        if shell.starttime is not None:
            birth = f"starttime:{shell.starttime}"
        else:
            birth = f"birth:{shell.birth!r}"
        built = pty_close_notices.closure_notice(
            machine=machine_name(),
            name=name,
            shell_pid=shell.pid,
            shell_birth=birth,
            operation=notice.operation,
            acquired_at=notice.acquired_at,
            reason=notice.reason,
            survivors=_named(left),
        )
        if built is not None:
            notices.append(built)
    for unwritten, exc in pty_close_notices.write_notices(notices, direct=direct_db):
        # The side-channel notice must never fail a closure; stay loud so the
        # gap is visible either way.
        print(
            f"closure notice for session {unwritten.name!r} (agent {unwritten.agent_id}) "
            f"could not be written: {type(exc).__name__}: {exc}",
            file=sys.stderr,
        )


def close_terminals(
    deadline: float, operation: str, acquired_at: datetime, *, direct_db: bool
) -> None:
    """Close this unit's terminals at `ava stop`: HUP/TERM, a bounded grace, then SIGKILL.

    The grace is `_TERMINAL_STOP_GRACE_S`, capped by the stop's `deadline`;
    the SIGKILL leg is bounded by `_TERMINAL_KILL_WAIT_S` and runs even when
    the grace spent the rest of the deadline — a stop that reached its
    terminal phase closes its terminals
    (decisions/2026-09-28-stop-escalates-to-sigkill.md). A process that
    outlives its SIGKILL fails the stop, which keeps its maintenance hold. A
    PTY host still tearing down after that gets the rest of the deadline, and
    at least `_TERMINAL_KILL_WAIT_S`, to clear its record.

    Busy sessions whose shell is verified gone — a job the SIGKILL cut short
    included — get a closure notice for their owner agent (issue #2044),
    written to the database over one short connection here, before the data
    plane stops (`direct_db`: this unit's own Postgres, bypassing its pooler,
    rather than the gateway's database a runner-only unit dials). That holds
    when a process outlived the SIGKILL too (the notice names it) and when
    another session keeps the stop incomplete (`_closed`).
    """
    _close(
        list(capture_terminals().terminals),
        _Notice(operation, acquired_at, pty_close_notices.STOP_REASON),
        direct_db=direct_db,
        grace_until=min(deadline, time.monotonic() + _TERMINAL_STOP_GRACE_S),
        kill_s=_TERMINAL_KILL_WAIT_S,
        stage="terminals",
        deadline=deadline,
    )


def stop_services(
    timeout: float, *, keep_terminals: bool = False, selected: frozenset[str] | None = None
) -> list[str]:
    """Ask the sole root owner to stop drained services without force escalation."""
    from cli.commands.lifecycle.root_driver import _root_tree_selection, _stop_root_service_tree

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


def report_postgres_stop_escalation(
    escalation: owned_postgres.Escalation, notes: list[str] | None = None
) -> None:
    """Report a Postgres shutdown that had to be ended by an immediate one.

    The owner (`base.cluster.postgres`) logs the escalation; this adds the
    operator line and the `postgres_stop_escalated` event. `notes`, when the
    caller owns a stop journal, collects the line for it
    (`_temporary_stop._finish_stop`); a leg that owns no journal passes
    nothing and still gets stderr and the event
    (decisions/2026-10-02-pg-stop-escalates-to-immediate.md).
    """
    killed = ", ".join(str(pid) for pid in escalation.killed) or "none"
    note = (
        f"postgres {escalation.detail}; ended by an immediate shutdown "
        f"(crash recovery at the next start; unarchived WAL stays in pg_wal), "
        f"killed leftover processes: {killed}"
    )
    print(f"  ! {note}", file=sys.stderr, flush=True)
    telemetry.emit(
        "telemetry",
        "postgres_stop_escalated",
        level="error",
        source="stop",
        attributes={"detail": escalation.detail, "killed": list(escalation.killed)},
    )
    if notes is not None:
        notes.append(note)


def stop_data_plane(
    timeout: float, *, save: bool = True, notes: list[str] | None = None
) -> list[str]:
    """Stop this home's native data plane; never stop a remote-managed plane.

    `notes` collects what the stop report must say (a Postgres shutdown that had to be
    escalated).
    """
    from cli.commands.data_plane.maintenance_stop import stop

    return stop(timeout, save=save, notes=notes)
