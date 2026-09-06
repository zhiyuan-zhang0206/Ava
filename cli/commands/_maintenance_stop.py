"""Stop a drained unit's recorded services without escalating to force.

The caller owns the maintenance journal and admission fence. These functions
prove only local recorded process identities; they do not prove remote drain or
stop OS-managed extras. Persistent terminals require a separate work boundary.
"""

from __future__ import annotations

import math
import os
import re
import time
from dataclasses import dataclass

import psutil

from shared.paths import run_dir
from shared.pty_sessions._paths import host_identity, host_starttime
from shared.session_backend import (
    SessionBackend,
    WinprocSessionBackend,
    get_backend,
    get_shell_backend,
)
from shared.session_record import SessionRecord, pid_starttime_ticks

_TERMINAL_NAME = re.compile(r"ava-(?:agent-\d+-shell-\d+(?:-|$)|schedule-\d+(?:-|$))")


@dataclass(frozen=True)
class OwnedProcess:
    pid: int
    birth: float
    starttime: int | None

    @classmethod
    def capture(cls, process: psutil.Process) -> OwnedProcess:
        return cls(process.pid, process.create_time(), pid_starttime_ticks(process.pid))

    def live(self) -> bool:
        try:
            process = psutil.Process(self.pid)
            if self.starttime is not None:
                actual = pid_starttime_ticks(self.pid)
                if actual is None:
                    raise RuntimeError(f"cannot verify process identity for PID {self.pid}")
                if actual != self.starttime:
                    return False
            elif process.create_time() != self.birth:
                return False
            return process.status() not in (psutil.STATUS_ZOMBIE, psutil.STATUS_DEAD)
        except psutil.NoSuchProcess:
            return False


def deadline_after(timeout: float) -> float:
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("maintenance stop timeout must be finite and positive")
    return time.monotonic() + timeout


def remaining(deadline: float) -> float:
    value = deadline - time.monotonic()
    if value <= 0:
        raise TimeoutError("maintenance kept its hold; stop deadline expired")
    return value


def capture_tree(identity: OwnedProcess) -> set[OwnedProcess]:
    """Capture descendants while the parent's birth identity still matches."""
    if not identity.live():
        return set()
    try:
        children = psutil.Process(identity.pid).children(recursive=True)
        captured = {identity}
        for child in children:
            try:
                captured.add(OwnedProcess.capture(child))
            except psutil.NoSuchProcess:
                continue
        # A PID replacement during enumeration invalidates the capture. Never
        # attach a replacement process's descendants to the original identity.
        if not identity.live():
            try:
                current = OwnedProcess.capture(psutil.Process(identity.pid))
            except psutil.NoSuchProcess:
                return captured
            if current.live():
                raise RuntimeError(f"process changed during descendant capture: {identity.pid}")
        return captured
    except psutil.NoSuchProcess:
        return {identity}


def wait_for_exit(
    tracked: set[OwnedProcess], deadline: float, *, groups: tuple[int, ...] = ()
) -> None:
    while True:
        living = {identity for identity in tracked if identity.live()}
        occupied = _occupied_groups(groups)
        if not living and not occupied:
            return
        for identity in living:
            tracked.update(capture_tree(identity))
        try:
            budget = remaining(deadline)
        except TimeoutError:
            raise TimeoutError(
                f"maintenance kept its hold; processes did not exit: "
                f"{sorted(identity.pid for identity in living)}; occupied process groups: {occupied}"
            ) from None
        time.sleep(min(0.05, budget))


def _occupied_groups(groups: tuple[int, ...]) -> list[int]:
    if not groups:
        return []
    occupied: set[int] = set()
    # killpg(..., 0) can report EPERM for an empty group on macOS. Read the
    # actual membership instead; an unreadable member cannot certify emptiness.
    for process in psutil.process_iter():
        try:
            group = os.getpgid(process.pid)
            if group in groups and process.status() not in (
                psutil.STATUS_ZOMBIE,
                psutil.STATUS_DEAD,
            ):
                occupied.add(group)
        except (psutil.NoSuchProcess, ProcessLookupError):
            continue
    return sorted(occupied)


def _capture_groups(records: dict[str, SessionRecord]) -> tuple[int, ...]:
    if os.name != "posix":
        return ()
    from shared.posixproc import _pgid_of

    captured: set[int | None] = set()
    for record in records.values():
        identity = OwnedProcess(record.pid, record.create_time, record.starttime)
        if not identity.live():
            raise RuntimeError("service identity changed before process-group capture")
        captured.add(_pgid_of(psutil.Process(record.pid)))
        if not identity.live():
            raise RuntimeError("service identity changed during process-group capture")
    if None in captured or os.getpgrp() in captured:
        raise RuntimeError("cannot verify an isolated service process group")
    return tuple(group for group in captured if group is not None)


def require_no_terminals() -> None:
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
    listed = backend.list_sessions()
    if isinstance(backend, WinprocSessionBackend):
        # Windows uses one record namespace for services and interactive shells.
        # These are the SDK and ScheduleManager's existing terminal name shapes.
        listed = [name for name in listed if _TERMINAL_NAME.match(name)]
    terminals.extend(listed)
    if terminals:
        raise RuntimeError(
            "persistent terminals/schedules require their own completed-work boundary; "
            f"maintenance will not kill or replay them: {sorted(set(terminals))}"
        )


def service_names(backend: SessionBackend, *, keep_terminals: bool = False) -> list[str]:
    """Select services from Windows' shared service/terminal record namespace."""
    names = backend.list_sessions()
    if keep_terminals and isinstance(backend, WinprocSessionBackend):
        names = [name for name in names if not _TERMINAL_NAME.match(name)]
    return sorted(names)


def _validate_service_records(backend: SessionBackend, *, keep_terminals: bool) -> None:
    # List APIs may discard malformed or stale records; do not let that erase
    # an identity uncertainty before strict preflight has examined it.
    for path in (run_dir() / "sessions").glob("*.json"):
        if (
            keep_terminals
            and isinstance(backend, WinprocSessionBackend)
            and _TERMINAL_NAME.match(path.stem)
        ):
            continue
        record = SessionRecord.read(path)
        if record is None:
            raise RuntimeError(f"cannot verify service record: {path.stem}")
        identity = OwnedProcess(record.pid, record.create_time, record.starttime)
        if not identity.live():
            try:
                current = OwnedProcess.capture(psutil.Process(record.pid))
            except psutil.NoSuchProcess:
                continue
            if current.live():
                raise RuntimeError(f"service identity changed before stop: {path.stem}")


def stop_services(timeout: float, *, keep_terminals: bool = False) -> list[str]:
    """Signal captured service identities; a survivor leaves maintenance held.

    No kill_session fallback is allowed. Windows uses the existing private
    console helper with the remaining budget and expected record identity.
    Original POSIX groups remain checked after their leaders exit; captured
    descendants that left those groups are followed by birth identity. Unknown
    newly daemonized sessions require separate ownership proof. Admission
    fencing and separately registered resources remain caller duties.
    keep_terminals is an operator assertion of a separately verified work
    boundary; it preserves terminals without proving they have stopped writing.
    """
    deadline = deadline_after(timeout)
    if not keep_terminals:
        require_no_terminals()
    backend = get_backend()
    _validate_service_records(backend, keep_terminals=keep_terminals)
    names = service_names(backend, keep_terminals=keep_terminals)
    records: dict[str, SessionRecord] = {}
    tracked: set[OwnedProcess] = set()
    ancestors = {os.getpid(), *(process.pid for process in psutil.Process().parents())}
    for name in names:
        remaining(deadline)
        record = SessionRecord.read(run_dir() / "sessions" / f"{name}.json")
        if record is None:
            raise RuntimeError(f"cannot read exact identity of service {name}")
        if record.pid in ancestors:
            raise RuntimeError("maintenance stop must run outside the unit's service tree")
        identity = OwnedProcess(record.pid, record.create_time, record.starttime)
        if not identity.live():
            raise RuntimeError(f"service identity changed before stop: {name}")
        records[name] = record
        tracked.update(capture_tree(identity))
    # The POSIX backend gives each service an isolated process group. Retain
    # it independently of the leader: a normal shutdown handler can fork and
    # exit before the next descendant snapshot. Never signal the group here.
    groups = _capture_groups(records)
    # Complete every identity/preflight check before delivering the first signal.
    if not keep_terminals:
        require_no_terminals()
    if service_names(backend, keep_terminals=keep_terminals) != names:
        raise RuntimeError("service roster changed before held stop")
    ordered = sorted(names, key=lambda name: not name.endswith(("watchdog", "restarter")))
    for name in ordered:
        remaining(deadline)
        if isinstance(backend, WinprocSessionBackend):
            signalled = backend.graceful_signal(
                name, expected=records[name], timeout=remaining(deadline)
            )
        else:
            signalled = backend.graceful_signal(name, expected=records[name])
        remaining(deadline)
        if not signalled:
            record = records[name]
            if OwnedProcess(record.pid, record.create_time, record.starttime).live():
                raise RuntimeError(f"graceful signal refused the captured service: {name}")
    wait_for_exit(tracked, deadline, groups=groups)
    remaining(deadline)
    if not keep_terminals:
        require_no_terminals()
    if service_names(backend, keep_terminals=keep_terminals):
        raise RuntimeError("services appeared during held stop")
    return names


def stop_data_plane(timeout: float) -> list[str]:
    """Stop this home's native data plane; never stop a remote-managed plane."""
    from cli.commands._maintenance_data_plane import stop

    return stop(timeout)
