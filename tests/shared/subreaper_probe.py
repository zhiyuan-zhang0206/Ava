"""Isolated Linux test process: never changes pytest's own subreaper state."""

import ctypes
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import psutil


def middle(root: Path) -> None:
    helper = subprocess.run(  # noqa: S603 — fixed module/command, isolated test paths
        [
            sys.executable,
            "-m",
            "shared._reparent",
            str(root / "out"),
            str(root / "err"),
            "/bin/sleep",
            "300",
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )
    child = psutil.Process(int(helper.stdout.strip()))
    sys.stdout.write(
        json.dumps({"pid": child.pid, "birth": child.create_time(), "caller": os.getpid()}) + "\n"
    )


def ancestor(root: Path) -> None:
    libc = ctypes.CDLL(None, use_errno=True)
    assert libc.prctl(36, 1, 0, 0, 0) == 0  # PR_SET_CHILD_SUBREAPER, this disposable process only
    caller = subprocess.run(  # noqa: S603 — execute this exact test file, no shell
        [sys.executable, __file__, "middle", str(root)],
        check=True,
        capture_output=True,
        text=True,
        timeout=15,
    )
    record = json.loads(caller.stdout)
    child = psutil.Process(record["pid"])
    try:
        deadline = time.monotonic() + 5
        while child.ppid() != os.getpid() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert child.create_time() == record["birth"]
        assert child.ppid() == os.getpid()
        assert child.status() != psutil.STATUS_ZOMBIE
        assert os.getsid(child.pid) != os.getsid(0)
        assert not psutil.pid_exists(record["caller"])
        record.update(
            adopter=os.getpid(),
            old_init_predicate=child.ppid() == 1,
            caller_exited=True,
            child_live=True,
            pgid=os.getpgid(child.pid),
            sid=os.getsid(child.pid),
            status=child.status(),
        )
    finally:
        # Only the exact child whose birth was captured; no name/namespace kill.
        if child.create_time() == record["birth"]:
            os.kill(child.pid, signal.SIGKILL)
            waited, _ = os.waitpid(child.pid, 0)
            assert waited == child.pid
    sys.stdout.write(json.dumps(record) + "\n")


if __name__ == "__main__":
    root = Path(sys.argv[2])
    if sys.argv[1] == "middle":
        middle(root)
    else:
        ancestor(root)
