"""Foreground postmasters kept in their owner's process group, and their families.

pg_ctl daemonizes its server. Cancellable restore workers instead retain the
postmaster as a direct child in their own process group. PostgreSQL's children
do not stay there: every one of them (checkpointer, background writer,
walwriter, the startup process, each backend) calls setsid() at birth, so a
group signal or an empty group proves nothing about them. They share their
parent while it lives, and their working directory, the data directory, for
their whole life.

A postmaster's family therefore closes in three steps:

1. its owner stops the postmaster cleanly: SIGQUIT (`pg_ctl stop -m
   immediate`) makes it signal every child and exit only after reaping them;
2. every birth recorded while the postmaster lived (itself and each validated
   descendant) must be dead; a survivor is killed by its exact native birth;
3. no live process keeps its working directory inside the data directory.

An operation worker publishes a receipt for every postmaster it starts
(`record_postmasters_in`), so the controller that closes the worker's group,
and a later retirement, can close and prove that family as well.
"""

from __future__ import annotations

import contextlib
import json
import math
import os
import signal
import subprocess
import time
from collections.abc import Callable, Collection, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import cast
from uuid import uuid4

import psutil
import psycopg

from shared.atomic_io import write_text_atomic
from shared.native_process import native_boot_id
from shared.native_process.ownership import OwnedProcess, capture_tree, retain_processes

# PostgreSQL SIGKILLs a child still alive five seconds into an immediate
# shutdown; past this bound its owner kills the recorded family itself.
POSTMASTER_SHUTDOWN_S = 10.0
_POLL_S = 0.05
_RECEIPT_GLOB = "postmaster-*.json"
# Births recorded while their parentage held, persisted beside the receipts so a
# later retirement can prove each of them dead without the recording process.
FAMILY_RECORD = "family.json"
_receipts: Path | None = None


def record_postmasters_in(directory: Path | None) -> None:
    """Publish a receipt into `directory` for every postmaster this process starts."""
    global _receipts  # noqa: PLW0603 -- one process-wide custody destination
    _receipts = directory


@dataclass(frozen=True)
class PostmasterReceipt:
    """One started postmaster: its data directory and, once born, its birth."""

    pgdata: Path
    boot_id: str
    postmaster: OwnedProcess | None

    def value(self) -> dict[str, object]:
        birth = None if self.postmaster is None else birth_value(self.postmaster)
        return {"pgdata": str(self.pgdata), "boot_id": self.boot_id, "postmaster": birth}


def read_postmaster_receipts(directory: Path) -> list[PostmasterReceipt]:
    """Every receipt in `directory`; a malformed one raises, it is never skipped."""
    receipts: list[PostmasterReceipt] = []
    for path in sorted(directory.glob(_RECEIPT_GLOB)):
        value = json.loads(path.read_text())
        if not isinstance(value, dict) or set(cast("dict[str, object]", value)) != {
            "pgdata",
            "boot_id",
            "postmaster",
        }:
            raise ValueError(f"malformed postmaster receipt {path.name}")
        record = cast("dict[str, object]", value)
        pgdata, boot, birth = record["pgdata"], record["boot_id"], record["postmaster"]
        if (
            not isinstance(pgdata, str)
            or not Path(pgdata).is_absolute()
            or not isinstance(boot, str)
        ):
            raise ValueError(f"malformed postmaster receipt {path.name}")
        receipts.append(PostmasterReceipt(Path(pgdata), boot, _birth(birth, path.name)))
    return receipts


def _birth(value: object, name: str) -> OwnedProcess | None:
    if value is None:
        return None
    if not isinstance(value, dict) or set(cast("dict[str, object]", value)) != {
        "pid",
        "birth",
        "starttime",
    }:
        raise ValueError(f"malformed postmaster birth in {name}")
    record = cast("dict[str, object]", value)
    pid, birth, ticks = record["pid"], record["birth"], record["starttime"]
    if (
        type(pid) is not int
        or pid <= 0
        or not isinstance(birth, (int, float))
        or isinstance(birth, bool)
        or not math.isfinite(birth)
        or birth <= 0
        or (ticks is not None and (type(ticks) is not int or ticks <= 0))
    ):
        raise ValueError(f"malformed postmaster birth in {name}")
    return OwnedProcess(pid, float(birth), ticks)


def _data_directory(argv: list[str]) -> Path:
    try:
        return Path(argv[argv.index("-D") + 1]).resolve()
    except (ValueError, IndexError) as exc:
        raise ValueError("a foreground postmaster names its data directory with -D") from exc


def _publish(path: Path, receipt: PostmasterReceipt) -> None:
    write_text_atomic(path, json.dumps(receipt.value(), sort_keys=True), mode=0o600)


def start_foreground_postgres(
    argv: list[str], *, log: Path, env: Mapping[str, str] | None = None
) -> subprocess.Popen[bytes]:
    """Transfer the child handle before readiness checks can fail or be cancelled.

    `env` is the child's environment (None inherits the caller's); the throwaway
    fixture passes `pg_start_env()` so the postmaster is never started without a
    locale (Task #3754). Under `record_postmasters_in`, the data directory is
    receipted before the launch and the postmaster's birth right after it; a
    postmaster whose birth cannot be receipted is killed before this raises.
    """
    receipt: Path | None = None
    pgdata, boot = Path(), None
    if _receipts is not None:
        pgdata, boot = _data_directory(argv), native_boot_id()
        if boot is None:
            raise RuntimeError("postmaster receipts need a native POSIX boot identity")
        receipt = _receipts / f"postmaster-{uuid4().hex}.json"
        _publish(receipt, PostmasterReceipt(pgdata, boot, None))
    with log.open("ab", buffering=0) as output:
        process = subprocess.Popen(  # noqa: S603 -- resolved postgres and caller-owned data directory
            argv, stdin=subprocess.DEVNULL, stdout=output.fileno(), stderr=output.fileno(), env=env
        )
    if receipt is not None and boot is not None:
        try:
            birth = OwnedProcess.capture(psutil.Process(process.pid))
            _publish(receipt, PostmasterReceipt(pgdata, boot, birth))
        except BaseException:
            process.kill()
            process.wait()
            raise
    return process


def wait_foreground_postgres(
    process: subprocess.Popen[bytes], *, log: Path, port: int, data: Path, timeout_s: float = 60
) -> None:
    """Accept only a live owned postmaster answering for the expected PGDATA."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"foreground Postgres exited {process.returncode}: {log}")
        try:
            with psycopg.connect(
                host="127.0.0.1",
                port=port,
                user="ava",
                dbname="postgres",
                connect_timeout=1,
                options="-c statement_timeout=1000",
            ) as connection:
                row = connection.execute("SHOW data_directory").fetchone()
        except psycopg.OperationalError:
            time.sleep(0.1)
            continue
        if row is None or Path(row[0]).resolve() != data.resolve():
            raise RuntimeError("foreground Postgres port is owned by another data directory")
        if process.poll() is not None:
            raise RuntimeError(f"foreground Postgres exited {process.returncode}: {log}")
        return
    raise TimeoutError(f"foreground Postgres did not become ready: {log}")


def postmaster_family(process: subprocess.Popen[bytes]) -> set[OwnedProcess]:
    """The live direct-child postmaster and every validated descendant, or nothing."""
    try:
        return capture_tree(OwnedProcess.capture(psutil.Process(process.pid)))
    except psutil.NoSuchProcess:
        return set()


def data_directory_holders(pgdatas: Collection[Path]) -> list[str]:
    """Live processes, besides this one and its ancestors, working inside a data directory.

    Every PostgreSQL process works in its data directory for its whole life,
    so none of a closed postmaster's family may be listed here.
    """
    if not pgdatas:
        return []
    ignored = {os.getpid(), *(parent.pid for parent in psutil.Process().parents())}
    holders: list[str] = []
    for process in psutil.process_iter(["pid", "name", "status"]):
        if process.pid in ignored or process.info["status"] in {
            psutil.STATUS_ZOMBIE,
            psutil.STATUS_DEAD,
        }:
            continue
        try:
            cwd = Path(process.cwd())
        except psutil.Error:
            continue  # gone, a zombie, or another user's process: not this family
        if any(cwd.is_relative_to(pgdata) for pgdata in pgdatas):
            holders.append(f"{process.pid} ({process.info['name']})")
    return holders


def close_family(
    recorded: set[OwnedProcess],
    pgdatas: Collection[Path],
    deadline: float,
    *,
    grew: Callable[[], None] = lambda: None,
) -> None:
    """Kill recorded survivors by exact birth until the family is provably gone.

    A live member's own descendants are recorded before it is signalled.
    Closure needs one observation with no recorded member live and no process
    working inside a data directory; `TimeoutError` names what remains.
    """
    while True:
        live = [member for member in recorded if member.live()]
        count = len(recorded)
        for member in live:
            retain_processes(recorded, capture_tree(member))
        if len(recorded) != count:
            grew()
        for member in recorded:
            if member.live():
                member.send_signal(signal.SIGKILL)
        holders = data_directory_holders(pgdatas)
        if not live and not holders:
            return
        if time.monotonic() >= deadline:
            remaining = [str(member.pid) for member in live] + holders
            raise TimeoutError(f"postgres family still has live members: {', '.join(remaining)}")
        time.sleep(_POLL_S)


def _record_until_exit(
    process: subprocess.Popen[bytes], family: set[OwnedProcess], timeout_s: float
) -> None:
    deadline = time.monotonic() + timeout_s
    while process.poll() is None and time.monotonic() < deadline:
        retain_processes(family, postmaster_family(process))
        time.sleep(_POLL_S)


def stop_foreground_postgres(process: subprocess.Popen[bytes]) -> None:
    """The owner's immediate shutdown; retain PGDATA if the direct child cannot die.

    This is for disposable clusters only. SIGQUIT stops every child; a
    postmaster that outlives POSTMASTER_SHUTDOWN_S is killed together with
    each descendant recorded while it lived. The recorded family must then
    be gone, even the members that setsid() put outside this process group.
    """
    family: set[OwnedProcess] = set()
    if process.poll() is None:
        family = postmaster_family(process)
        process.send_signal(signal.SIGQUIT)
        _record_until_exit(process, family, POSTMASTER_SHUTDOWN_S)
        if process.poll() is None:
            retain_processes(family, postmaster_family(process))
            for member in family:
                member.send_signal(signal.SIGKILL)
            process.kill()
    process.wait(timeout=2)
    close_family(family, (), time.monotonic() + 5)


def birth_value(member: OwnedProcess) -> dict[str, object]:
    """One recorded birth in its persisted receipt form."""
    return {"pid": member.pid, "birth": member.birth, "starttime": member.starttime}


def recorded_family(values: Iterable[object]) -> set[OwnedProcess]:
    """Births persisted by `close_family` callers; a malformed one raises."""
    family: set[OwnedProcess] = set()
    for value in values:
        member = _birth(value, "a family record")
        if member is None:
            raise ValueError("a family record names no birth")
        family.add(member)
    return family


class FamilyCustody:
    """Every process a worker group can leave behind, closed by exact birth.

    `directory` holds the worker's postmaster receipts. Births are recorded
    from the worker's tree and from each receipted postmaster still in the
    pinned `group`, while their parentage holds, and persisted in
    `FAMILY_RECORD` for a later `family_refusal`.
    """

    def __init__(self, directory: Path, group: int, leader: OwnedProcess | None) -> None:
        self.directory = directory
        self.group = group
        self.leader = leader
        # A retried closure keeps proving every birth an earlier attempt recorded.
        self.recorded = _persisted_family(directory)

    def _persist(self) -> None:
        members = sorted(self.recorded, key=lambda member: member.pid)
        write_text_atomic(
            self.directory / FAMILY_RECORD,
            json.dumps({"boot_id": native_boot_id(), "members": [birth_value(m) for m in members]}),
            mode=0o600,
        )

    def _record(self, roots: Iterable[OwnedProcess]) -> None:
        count = len(self.recorded)
        for root in roots:
            retain_processes(self.recorded, capture_tree(root))
        if len(self.recorded) != count:
            self._persist()

    def _postmasters(self) -> list[OwnedProcess]:
        """Receipted postmasters still live inside the pinned group."""
        owned: list[OwnedProcess] = []
        for receipt in read_postmaster_receipts(self.directory):
            member = receipt.postmaster
            if member is None or not member.live():
                continue
            with contextlib.suppress(ProcessLookupError):
                if os.getpgid(member.pid) == self.group:
                    owned.append(member)
        return owned

    def stop_postgres(self, deadline: float) -> None:
        """The owner's clean stop: immediate shutdown of each live postmaster.

        The postmaster signals every child and exits only after reaping them;
        births are recorded until it exits, so a child it forked meanwhile is
        still known if it has to be killed.
        """
        postmasters = self._postmasters()
        self._record([*([] if self.leader is None else [self.leader]), *postmasters])
        for postmaster in postmasters:
            postmaster.send_signal(signal.SIGQUIT)
        while (live := [p for p in postmasters if p.live()]) and time.monotonic() < deadline:
            self._record(live)
            time.sleep(_POLL_S)

    def close(self, deadline: float) -> None:
        """After the group closed: kill recorded survivors, prove every family gone."""
        pgdatas = [receipt.pgdata for receipt in read_postmaster_receipts(self.directory)]
        close_family(self.recorded, pgdatas, deadline, grew=self._persist)


def family_refusal(directory: Path) -> str | None:
    """Why a recorded birth or a receipted postgres family may still be alive.

    The retirement form of `FamilyCustody.close`, which never signals: every
    recorded birth and every receipted postmaster must be dead, and no
    process may still work inside a receipted data directory.
    """
    receipts = read_postmaster_receipts(directory)
    members = _persisted_family(directory)
    members.update(r.postmaster for r in receipts if r.postmaster is not None)
    live = sorted(member.pid for member in members if member.live())
    if live:
        return f"recorded postgres family members still run: {live}"
    holders = data_directory_holders([receipt.pgdata for receipt in receipts])
    if holders:
        return f"processes still work inside a receipted data directory: {holders}"
    return None


def _persisted_family(directory: Path) -> set[OwnedProcess]:
    if not (directory / FAMILY_RECORD).exists():
        return set()
    return recorded_family(json.loads((directory / FAMILY_RECORD).read_text())["members"])
