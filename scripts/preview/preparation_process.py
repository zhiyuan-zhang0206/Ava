"""Finite Linux preparation custody, including nested ordinary process groups.

The caller owns one session directly. Its unreaped leader pins the session ID;
pidfds bind signals to native tasks instead of recyclable group/PID numbers.
This is not a sandbox for trusted build tools that deliberately call setsid.
"""

from __future__ import annotations

import os
import select
import signal
import subprocess
import sys
import time
from collections.abc import Callable, Generator
from contextlib import contextmanager, suppress
from dataclasses import asdict, dataclass
from pathlib import Path
from types import FrameType
from typing import Any

from scripts.preview.local import write_json
from shared.native_process import pidfd

_CLOSE_SECONDS = 5.0
_POLL_SECONDS = 0.05
# Keep direct children unreaped after uncertain closure. No subsequent Popen
# garbage collection may accidentally retire the session identity in this owner.
_UNRESOLVED: list[subprocess.Popen[bytes]] = []


@dataclass(frozen=True)
class Member:
    pid: int
    parent: int
    group: int
    session: int
    starttime: int
    state: str


@contextmanager
def _defer_cancellation() -> Generator[Callable[[], None]]:
    """Keep spawn/admission and closure atomic with respect to CLI cancellation.

    Python handlers defer the exception, not the native signal mask: executed
    tools keep the caller's ordinary signal mask.
    """
    pending: list[int] = []

    def capture(signum: int, _frame: FrameType | None) -> None:
        pending.append(signum)

    def check() -> None:
        if pending:
            raise KeyboardInterrupt(f"preparation cancelled by signal {pending[0]}")

    previous: dict[int, Callable[[int, FrameType | None], Any] | int | None] = {}
    try:
        for signum in (signal.SIGINT, signal.SIGTERM):
            previous[signum] = signal.signal(signum, capture)
        yield check
        check()
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)


def _member(pid: int) -> Member:
    contents = (Path("/proc") / str(pid) / "stat").read_text()
    fields = contents[contents.rindex(")") + 2 :].split()
    return Member(pid, int(fields[1]), int(fields[2]), int(fields[3]), int(fields[19]), fields[0])


def _members(session: int) -> list[Member]:
    found: list[Member] = []
    for path in Path("/proc").iterdir():
        if not path.name.isdecimal():
            continue
        try:
            member = _member(int(path.name))
        except (FileNotFoundError, ProcessLookupError):
            continue
        # Permission/read errors remain unknown; never reinterpret them as absence.
        if member.session == session and member.state not in {"Z", "X"}:
            found.append(member)
    return found


def _ended(fd: int) -> bool:
    return bool(select.select([fd], [], [], 0)[0])


def _signal(member: Member) -> None:
    if sys.platform != "linux":
        raise RuntimeError("finite preparation requires Linux pidfd custody")
    try:
        fd = pidfd.open_process(member.pid)
    except ProcessLookupError:
        return
    try:
        # The handle may have opened after PID turnover. Check membership/birth
        # again, and require the referenced task to remain live across that read.
        if _ended(fd):
            return
        try:
            current = _member(member.pid)
        except (FileNotFoundError, ProcessLookupError):
            return
        if (current.session, current.starttime) != (member.session, member.starttime):
            raise RuntimeError("preparation member changed before native signal")
        if not _ended(fd):
            with suppress(ProcessLookupError):
                pidfd.send_signal(fd, signal.SIGKILL)
    finally:
        os.close(fd)


def _wait(
    process: subprocess.Popen[bytes], leader_fd: int, timeout: float, cancelled: Callable[[], None]
) -> None:
    deadline = time.monotonic() + timeout
    while not (_ended(leader_fd) and not _members(process.pid)):
        cancelled()
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise subprocess.TimeoutExpired(process.args, timeout)
        time.sleep(min(_POLL_SECONDS, remaining))


def _close(process: subprocess.Popen[bytes], leader_fd: int) -> None:
    deadline = time.monotonic() + _CLOSE_SECONDS
    while True:
        members = _members(process.pid)
        if not members and _ended(leader_fd):
            return
        if time.monotonic() >= deadline:
            raise TimeoutError("preparation session closure remains unresolved")
        for member in members:
            _signal(member)
        time.sleep(_POLL_SECONDS)


def _finish(
    process: subprocess.Popen[bytes],
    leader_fd: int,
    timeout: float,
    record: dict[str, Any],
    cancelled: Callable[[], None],
) -> None:
    try:
        cancelled()
        _wait(process, leader_fd, timeout, cancelled)
        cancelled()
    except BaseException as original:
        record.update(result="failed", error=repr(original))
        try:
            record["members_at_failure"] = [asdict(member) for member in _members(process.pid)]
            _close(process, leader_fd)
        except BaseException as closure:
            record.update(custody="unresolved", closure_error=repr(closure))
            _UNRESOLVED.append(process)
            raise RuntimeError(
                f"preparation failed ({original!r}); native session custody unresolved ({closure!r})"
            ) from original
        record.update(custody="closed", returncode=process.wait())
        raise
    code = process.wait()  # Only after leader exit and positively empty live session.
    record.update(custody="closed", returncode=code, result="passed" if code == 0 else "failed")
    if code:
        raise subprocess.CalledProcessError(code, process.args)


def run(
    argv: list[str], *, cwd: Path, env: dict[str, str], log: Path, evidence: Path, timeout: float
) -> None:
    """Direct finite owner; never wrap this function in a separately timed command."""
    if sys.platform != "linux":
        raise RuntimeError("finite preparation requires Linux pidfd custody")
    pidfd.require_available()
    if signal.getsignal(signal.SIGCHLD) != signal.SIG_DFL:
        raise RuntimeError("finite preparation requires an unreaped direct child")
    record: dict[str, Any] = {"result": "running", "custody": "not-started", "argv": argv}
    if evidence.exists():
        raise FileExistsError("preparation custody evidence already exists")
    write_json(evidence, record)
    with _defer_cancellation() as cancelled, log.open("xb") as output:
        process = None
        leader_fd = None
        try:
            cancelled()
            record["custody"] = "unresolved"
            write_json(evidence, record)
            try:
                process = subprocess.Popen(  # noqa: S603 -- explicit finite argv, no shell.
                    argv,
                    cwd=cwd,
                    env=env,
                    stdin=subprocess.DEVNULL,
                    stdout=output,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                )
            except FileNotFoundError:
                record["custody"] = "not-started"
                raise
            record["session"] = process.pid
            leader_fd = pidfd.open_process(process.pid)
            record["leader"] = asdict(_member(process.pid))
            write_json(evidence, record)
            _finish(process, leader_fd, timeout, record, cancelled)
        except BaseException as exc:
            record.update(result="failed", error=repr(exc))
            raise
        finally:
            if process is not None and record["custody"] != "closed" and process not in _UNRESOLVED:
                _UNRESOLVED.append(process)
            if leader_fd is not None:
                os.close(leader_fd)
            write_json(evidence, record)
