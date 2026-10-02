"""Durable custody for the home's directly launched PostgreSQL postmaster.

A pidfile describes PostgreSQL, but cannot grant native process authority. Only
this launch boundary records the exact child birth. Interrupted admission stays
unknown; ordinary observation never adopts a process from a pidfile.
"""

from __future__ import annotations

import math
import os
import signal
import subprocess
import sys
import time
from collections.abc import Callable, Generator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from types import FrameType
from typing import Any, Literal, cast

import psutil
from pydantic import Field, model_validator

from base.deploy.release.verified_file import regular_bytes
from base.host.atomic_io import write_text_atomic
from base.host.private_storage import ensure_private_dir
from base.log import logger
from base.native_process import native_boot_id
from base.native_process.evidence import EvidenceModel, ExpectedProcess
from base.native_process.os_platform import file_lock
from base.native_process.ownership import OwnedProcess, capture_tree, retain_processes

# Retain unreaped direct children through admission, including ambiguous failure.
_CHILDREN: list[subprocess.Popen[bytes]] = []


class Receipt(EvidenceModel):
    version: Literal[1] = 1
    data: str
    directory: tuple[int, int]
    port: int = Field(gt=0, lt=65536)
    platform: Literal["linux", "darwin"]
    boot_id: str = Field(min_length=1)
    state: Literal["pending", "captured", "ready", "not-started"]
    owner: ExpectedProcess | None = None
    pidfile: tuple[int, int] | None = None
    header: tuple[str, ...] | None = None

    @model_validator(mode="after")
    def coherent(self) -> Receipt:
        if (self.owner is not None) != (self.state in {"captured", "ready"}):
            raise ValueError("PostgreSQL receipt state and native birth disagree")
        if (self.pidfile is not None or self.header is not None) != (self.state == "ready"):
            raise ValueError("PostgreSQL ready receipt requires its bound pidfile")
        if self.state == "ready" and (self.pidfile is None or self.header is None):
            raise ValueError("PostgreSQL pidfile binding is incomplete")
        if self.platform == "linux" and self.owner is not None and self.owner.starttime is None:
            raise ValueError("PostgreSQL Linux custody requires native start ticks")
        return self

    def process(self) -> OwnedProcess:
        if self.owner is None:
            raise RuntimeError("PostgreSQL launch has no captured native birth; reconcile custody")
        return OwnedProcess(self.owner.pid, self.owner.create_time, self.owner.starttime)


def _data_path(data: Path) -> Path:
    canonical = data.parent.resolve() / data.name
    if canonical.is_symlink():
        raise RuntimeError("PostgreSQL data directory cannot be a symlink into another home")
    return canonical


def receipt_path(data: Path) -> Path:
    return _data_path(data).parent / "run" / "postgres.json"


def _inode(path: Path) -> tuple[int, int]:
    info = path.stat()
    return info.st_dev, info.st_ino


def _boot() -> str:
    if sys.platform not in {"linux", "darwin"}:
        raise RuntimeError("owned PostgreSQL requires a supported POSIX platform")
    boot = native_boot_id()
    if boot is None:
        raise RuntimeError("cannot observe PostgreSQL native boot scope")
    return boot


def _write(data: Path, receipt: Receipt) -> None:
    path = receipt_path(data)
    ensure_private_dir(path.parent)
    write_text_atomic(path, receipt.model_dump_json() + "\n", sync_parent=True)


def _read(data: Path) -> Receipt | None:
    path = receipt_path(data)
    try:
        raw = regular_bytes(path, max_bytes=16 * 1024)
    except FileNotFoundError:
        return None
    receipt = Receipt.model_validate_json(raw)
    if receipt.data != str(data) or receipt.platform != sys.platform:
        raise RuntimeError("PostgreSQL custody names another data directory or platform")
    return receipt


def _directory_matches(process: psutil.Process, data: Path, receipt: Receipt | None) -> bool:
    cwd = Path(process.cwd())
    if cwd.resolve() == data:
        return True
    if receipt is not None and _inode(cwd) == receipt.directory:
        return True
    argv = process.cmdline()
    return "-D" in argv and Path(argv[argv.index("-D") + 1]).resolve() == data


def _require_closed(data: Path, receipt: Receipt | None) -> None:
    """A dead postmaster alone cannot prove that its old workers have closed."""
    session = (
        receipt.owner.pid
        if receipt is not None and receipt.owner is not None and receipt.boot_id == _boot()
        else None
    )
    for process in psutil.process_iter(["pid"]):
        try:
            if process.uids().real != os.getuid() or process.status() in {
                psutil.STATUS_ZOMBIE,
                psutil.STATUS_DEAD,
            }:
                continue
            pg_named = process.name() in {"postgres", "postmaster"}
            # Include archive commands and other children with non-PG names.
            # A prior boot never donates its recycled session ID as authority.
            if (session is not None and os.getsid(process.pid) == session) or (
                pg_named and _directory_matches(process, data, receipt)
            ):
                raise RuntimeError("PostgreSQL descendants or an unrecorded process retain custody")
        except (psutil.NoSuchProcess, ProcessLookupError):
            continue


def _pidfile(data: Path, receipt: Receipt) -> tuple[tuple[int, int], tuple[str, ...]]:
    path = data / "postmaster.pid"
    before = _inode(path)
    lines = regular_bytes(path, max_bytes=8192).decode().splitlines()
    owner = receipt.process()
    if (
        len(lines) < 4
        or int(lines[0]) != owner.pid
        or Path(lines[1]).resolve() != data
        or int(lines[3]) != receipt.port
        or before != _inode(path)
    ):
        raise RuntimeError("PostgreSQL pidfile does not describe the captured native child")
    # The wall timestamp is bound as file data, never compared to native birth.
    header = tuple(lines[:4])
    if receipt.pidfile is not None and (receipt.pidfile, receipt.header) != (before, header):
        raise RuntimeError("PostgreSQL pidfile changed after native admission")
    return before, header


def _require_process(data: Path, receipt: Receipt) -> OwnedProcess:
    owner = receipt.process()
    process = psutil.Process(owner.pid)
    argv = process.cmdline()
    if (
        _inode(data) != receipt.directory
        or process.name() not in {"postgres", "postmaster"}
        or "-D" not in argv
        or Path(argv[argv.index("-D") + 1]).resolve() != data
        or Path(process.cwd()).resolve() != data
        or os.getsid(owner.pid) != owner.pid
        or not owner.same_birth(OwnedProcess.capture(process))
        or not owner.live()
    ):
        raise RuntimeError("cannot verify this home's captured PostgreSQL process")
    return owner


def observe(data: Path) -> OwnedProcess | None:
    """Read exact launch evidence. Missing, interrupted or unreadable is not adoption."""
    data = _data_path(data)
    receipt = _read(data)
    if receipt is None:
        if (data / "postmaster.pid").exists():
            raise RuntimeError("PostgreSQL pidfile has no native launch receipt; reconcile custody")
        _require_closed(data, None)
        return None
    if receipt.state == "pending":
        raise RuntimeError("PostgreSQL launch custody is unresolved; reconcile pending receipt")
    if receipt.boot_id != _boot() or receipt.owner is None or not receipt.process().live():
        _require_closed(data, receipt)
        return None
    owner = _require_process(data, receipt)
    _pidfile(data, receipt)
    if not owner.live():
        raise RuntimeError("PostgreSQL exited during custody observation")
    return owner


@contextmanager
def _admission() -> Generator[Callable[[], None]]:
    """Defer Python cancellation until native custody is durable, without masking children."""
    pending: list[int] = []
    previous: dict[int, Callable[[int, FrameType | None], Any] | int | None] = {}

    def capture(signum: int, _frame: FrameType | None) -> None:
        pending.append(signum)

    def check() -> None:
        if pending:
            raise KeyboardInterrupt(f"PostgreSQL admission interrupted by signal {pending[0]}")

    try:
        for signum in (signal.SIGINT, signal.SIGTERM):
            previous[signum] = signal.signal(signum, capture)
        yield check
        check()
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)


def _spawn(data: Path, receipt: Receipt, argv: list[str], env: dict[str, str]) -> Receipt:
    _write(data, receipt)
    with _admission(), (data / "pg.log").open("ab") as log:
        try:
            child = subprocess.Popen(  # noqa: S603 — explicit resolved PostgreSQL argv, never a shell
                argv,
                cwd=data,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=log,
                start_new_session=True,
            )
        except OSError:
            # Popen's normal exec error guarantees it reaped its child. A
            # BaseException or later error has no such guarantee: keep pending.
            _write(data, receipt.model_copy(update={"state": "not-started"}))
            raise
        _CHILDREN.append(child)
        owner = OwnedProcess.capture(psutil.Process(child.pid))
        owner.birth_key()
        captured = receipt.model_copy(
            update={
                "state": "captured",
                "owner": ExpectedProcess(
                    pid=owner.pid, create_time=owner.birth, starttime=owner.starttime
                ),
            }
        )
        _write(data, captured)
        return captured


def _complete_start(
    data: Path,
    receipt: Receipt,
    ready: Callable[[], bool],
    timeout: float,
) -> OwnedProcess:
    """One readiness and durable admission boundary for fresh and retained births."""
    from base.cluster.ownership import require_listener

    owner = receipt.process()
    deadline = time.monotonic() + timeout
    while owner.live():
        protocol_ready = ready()
        if time.monotonic() >= deadline:
            raise RuntimeError("PostgreSQL did not become ready; captured custody is retained")
        if protocol_ready:
            _require_process(data, receipt)
            inode, header = _pidfile(data, receipt)
            require_listener(owner, receipt.port)
            admitted = receipt.model_copy(
                update={"state": "ready", "pidfile": inode, "header": header}
            )
            if admitted != receipt:
                _write(data, admitted)
            return owner
        time.sleep(0.05)
    _require_closed(data, receipt)
    raise RuntimeError(f"PostgreSQL exited before readiness; inspect {data / 'pg.log'}")


def _retained_start(data: Path, port: int, expected: OwnedProcess) -> Receipt:
    owner = observe(data)
    if owner is None or not owner.same_birth(expected):
        raise RuntimeError("PostgreSQL identity changed before retained startup")
    receipt = _read(data)
    if receipt is None or receipt.port != port:
        raise RuntimeError("PostgreSQL retained receipt does not match the startup port")
    if not owner.send_signal(signal.SIGHUP):
        raise RuntimeError("PostgreSQL exited before retained startup")
    return receipt


def start(
    data: Path,
    port: int,
    argv: list[str],
    env: dict[str, str],
    *,
    ready: Callable[[], bool],
    timeout: float = 60,
    expected: OwnedProcess | None = None,
) -> OwnedProcess:
    """Complete readiness for a new child or the exact retained postmaster.

    The protocol callback must itself be bounded. A retained birth is never
    replaced, and captured admission becomes ready only after the same checks
    as a fresh launch. Every path runs under the home PG control lock.
    """
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("PostgreSQL startup timeout must be finite and positive")
    data = _data_path(data)
    data.stat()
    path = receipt_path(data)
    ensure_private_dir(path.parent)
    with file_lock(path.with_suffix(".lock"), timeout_s=timeout):
        if expected is not None:
            retained = _retained_start(data, port, expected)
            return _complete_start(data, retained, ready, timeout)
        if observe(data) is not None:
            raise RuntimeError("PostgreSQL is already running; startup cannot replace its birth")
        from base.cluster.ownership import require_listener

        require_listener(None, port, required=False)
        captured = _spawn(
            data,
            Receipt(
                data=str(data),
                directory=_inode(data),
                port=port,
                platform=cast("Literal['linux', 'darwin']", sys.platform),
                boot_id=_boot(),
                state="pending",
            ),
            argv,
            env,
        )
        return _complete_start(data, captured, ready, timeout)


@dataclass(frozen=True)
class Escalation:
    """A fast shutdown that did not finish and was ended by an immediate one."""

    detail: str
    killed: tuple[int, ...]  # pids SIGKILLed after the immediate shutdown (verified descendants)


def _wait_all_gone(members: set[OwnedProcess], wait: float) -> bool:
    deadline = time.monotonic() + wait
    while any(member.live() for member in members):
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.05)
    return True


def _escalate(
    owner: OwnedProcess,
    tree: set[OwnedProcess],
    fast_wait: float,
    immediate_wait: float,
    kill_wait: float,
) -> Escalation:
    """End a fast shutdown that is stuck: SIGQUIT, then SIGKILL what outlives it.

    Postgres stays stuck in a fast shutdown while its archiver waits on an archive command
    that never returns. SIGQUIT is Postgres' immediate shutdown (no checkpoint; the next
    start replays WAL, and segments not yet archived stay in `pg_wal` for the archiver).
    The archiver dies with it but its archive command, a child of the archiver, is
    orphaned, so the tree captured before the signal is the only proof of whose it is:
    every survivor still matching its recorded birth is SIGKILLed, children before the
    postmaster, and nothing is ever signalled by name or pid alone.
    """
    detail = (
        f"fast shutdown did not complete within {fast_wait:.0f}s "
        "(a blocked archive command is the usual cause)"
    )
    logger.error(f"[postgres] {detail}; escalating to immediate shutdown")
    for member in tuple(tree):  # a command the archiver started after the first capture
        retain_processes(tree, capture_tree(member))
    owner.send_signal(signal.SIGQUIT)
    _wait_all_gone({owner}, immediate_wait)
    survivors = sorted(
        (member for member in tree if member.live()), key=lambda member: member == owner
    )
    for member in survivors:
        member.send_signal(signal.SIGKILL)
    if not _wait_all_gone(tree, kill_wait):
        alive = sorted(member.pid for member in tree if member.live())
        logger.error(f"[postgres] processes outlived SIGKILL: {alive}")
        raise RuntimeError(
            f"PostgreSQL immediate shutdown left processes {alive} alive; custody retained"
        )
    killed = tuple(member.pid for member in survivors)
    logger.error(f"[postgres] immediate shutdown done; killed leftover processes {list(killed)}")
    return Escalation(detail, killed)


def stop(
    data: Path,
    *,
    expected: OwnedProcess | None = None,
    timeout: float = 60,
    immediate_wait: float | None = None,
    kill_wait: float | None = None,
) -> Escalation | None:
    """Fast clean shutdown, followed by exact native tree and endpoint closure.

    A fast shutdown that has not finished after `timeout` fails with custody retained,
    unless `immediate_wait` and `kill_wait` are given: then it is ended by an immediate
    shutdown (waiting at most `immediate_wait` for the postmaster) and a SIGKILL of the
    recorded descendants that outlive it (waiting at most `kill_wait`), and the returned
    `Escalation` says so. Returns None for a shutdown that finished by itself.
    """
    data = _data_path(data)
    path = receipt_path(data)
    ensure_private_dir(path.parent)
    with file_lock(path.with_suffix(".lock"), timeout_s=timeout):
        owner = observe(data)
        receipt = _read(data)
        from base.cluster.ownership import require_listener

        if owner is None:
            if receipt is not None:
                require_listener(None, receipt.port, required=False)
            return None
        if expected is not None and not owner.same_birth(expected):
            raise RuntimeError("PostgreSQL replacement cannot inherit retained stop authority")
        if receipt is None:
            raise RuntimeError("PostgreSQL receipt disappeared before native control")
        require_listener(owner, receipt.port, required=False)
        tree = capture_tree(owner)
        owner.send_signal(signal.SIGINT)  # PostgreSQL fast, checkpointed shutdown.
        deadline = time.monotonic() + timeout
        escalation: Escalation | None = None
        while any(member.live() for member in tree):
            if time.monotonic() >= deadline:
                if immediate_wait is None or kill_wait is None:
                    raise RuntimeError(
                        "PostgreSQL native shutdown did not complete; custody retained"
                    )
                escalation = _escalate(owner, tree, timeout, immediate_wait, kill_wait)
                break
            time.sleep(0.05)
        _require_closed(data, receipt)
        require_listener(None, receipt.port, required=False)
        for child in tuple(_CHILDREN):
            if child.pid == owner.pid:
                child.wait(timeout=1)
                _CHILDREN.remove(child)
        return escalation
