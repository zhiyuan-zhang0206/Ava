"""Publish a process session only after winning the runtime admission CAS.

The launcher's attempt record is not the canonical agent record. A losing boot
never reaches this module; a winning boot publishes before committing admission
and before importing the execution graph. No process is signalled here.
"""

import os
import shlex
import sys
import time
from pathlib import Path

import psutil

from shared.cluster import session_name
from shared.paths import run_dir
from shared.platform import file_lock
from shared.runtime_incarnation import RuntimeIncarnation
from shared.session_record import SessionRecord, pid_starttime_ticks


def _may_replace(record: SessionRecord, current: psutil.Process) -> bool:
    """Require positive exit/replacement evidence; an unreadable process is live."""
    try:
        previous = psutil.Process(record.pid)
        if previous.status() == psutil.STATUS_ZOMBIE:
            return True
        if record.starttime is not None:
            matches = record.identifies(record.pid)
            if matches is None:
                raise RuntimeError("canonical agent session identity is unreadable")
        else:
            if record.create_time <= 0:
                raise RuntimeError("canonical agent session birth identity is unknown")
            matches = previous.create_time() == record.create_time
        if not matches:
            return True
        return previous.pid == current.pid and previous.create_time() == current.create_time()
    except psutil.NoSuchProcess:
        return True


def publish_admitted_session(incarnation: RuntimeIncarnation) -> None:
    """Called while holding the winning agents_meta row lock, before COMMIT.

    A failed write rolls admission back. A crash after the write but before
    COMMIT leaves a record whose dead process can be replaced by a later winner.
    The bounded file lock only covers local identity reads and atomic writing;
    it never waits for another process to exit.
    """
    path = run_dir() / "sessions" / f"{session_name(f'agent-{incarnation.agent_id}')}.json"
    current = psutil.Process(os.getpid())
    with file_lock(path.with_suffix(".admission.lock"), timeout_s=1.0):
        previous = SessionRecord.read(path)
        if previous is None and path.exists():
            raise RuntimeError("canonical agent session record is unreadable")
        if previous is not None and not _may_replace(previous, current):
            raise RuntimeError("canonical agent session still belongs to a live process")
        SessionRecord(
            pid=current.pid,
            create_time=current.create_time(),
            cmd=shlex.join(sys.argv),
            cwd=str(Path.cwd()),
            started_at=time.time(),
            starttime=pid_starttime_ticks(current.pid),
            generation=str(incarnation.generation),
        ).write(path)
