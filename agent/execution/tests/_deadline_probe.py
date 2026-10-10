"""Isolated subprocess probes; never imported by a production entry."""

from __future__ import annotations

import contextlib
import ctypes
import json
import os
import select
import signal
import subprocess
import sys
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import psutil

from agent.execution import owner_child
from base.agents.incarnation.exec_owner_protocol import OwnerControl, read_owner_context


def _wait_file(path: Path) -> None:
    deadline = time.monotonic() + 15
    while not path.exists():
        if time.monotonic() >= deadline:
            raise TimeoutError("private native entry was never reached")
        time.sleep(0.01)


def _parent(context_path: Path) -> None:
    context = read_owner_context(context_path)
    child = psutil.Popen(
        [
            sys.executable,
            "-I",
            "-B",
            "-m",
            "agent.execution.owner_child",
            "--context",
            str(context_path),
        ],
        stdin=subprocess.PIPE,
    )
    assert child.stdin is not None
    permit = OwnerControl(
        request=context.allocation.request, domain=context.allocation.domain, action="permit"
    )
    child.stdin.write(permit.model_dump_json().encode() + b"\n")
    child.stdin.flush()
    context_path.with_suffix(".pid").write_text(str(child.pid))
    child.wait(timeout=20)


def _exit_status(pid: int, watch: select.kqueue | None) -> int:
    if sys.platform == "darwin":
        assert watch is not None
        events = watch.control(None, 1, 15)
        assert len(events) == 1 and events[0].fflags & select.KQ_NOTE_EXIT
        return os.waitstatus_to_exitcode(events[0].data)
    deadline = time.monotonic() + 15
    while True:
        ended, status = os.waitpid(pid, os.WNOHANG)
        if ended:
            return os.waitstatus_to_exitcode(status)
        if time.monotonic() >= deadline:
            raise TimeoutError("private orphan watchdog never exited")
        time.sleep(0.01)


def _orphan(context_path: Path, ready: Path) -> None:
    # Subreaper custody is local to this isolated Linux probe, never pytest/service.
    if sys.platform == "linux":
        libc = ctypes.CDLL(None, use_errno=True)
        if libc.prctl(36, 1, 0, 0, 0) != 0:  # PR_SET_CHILD_SUBREAPER
            raise OSError(ctypes.get_errno(), "private subreaper admission failed")
    parent = psutil.Popen(
        [sys.executable, "-I", "-B", __file__, "parent", str(context_path), str(ready)]
    )
    watch = None
    child_identity = None
    try:
        _wait_file(context_path.with_suffix(".pid"))
        pid = int(context_path.with_suffix(".pid").read_text())
        child_identity = psutil.Process(pid)
        birth = child_identity.create_time()
        _wait_file(ready)
        assert child_identity.is_running() and parent.poll() is None
        if sys.platform == "darwin":
            watch = select.kqueue()
            event = select.kevent(
                pid,
                filter=select.KQ_FILTER_PROC,
                flags=select.KQ_EV_ADD | select.KQ_EV_ONESHOT,
                # XNU sys/event.h: status is observable for a PID we may signal.
                fflags=select.KQ_NOTE_EXIT | 0x04000000,  # NOTE_EXITSTATUS
            )
            watch.control([event], 0, 0)
        parent.kill()
        assert parent.wait(timeout=5) == -signal.SIGKILL
        code = _exit_status(pid, watch)
        sys.stdout.write(
            json.dumps({"exit_code": code, "parent_exit": parent.returncode, "child_birth": birth})
            + "\n"
        )
    finally:
        if watch is not None:
            watch.close()
        if parent.poll() is None:
            parent.kill()
            parent.wait(timeout=5)
        if child_identity is not None:
            with contextlib.suppress(psutil.NoSuchProcess):
                if child_identity.status() not in {psutil.STATUS_ZOMBIE, psutil.STATUS_DEAD}:
                    child_identity.kill()


def _entry(mode: str, context: Path, ready: Path) -> None:
    if mode in {"normal", "cleanup", "refused"}:

        def payload(_name: str, *, run_name: str) -> dict[str, object]:
            assert run_name == "__main__"
            assert any(t.name == "exec-original-deadline" for t in threading.enumerate())
            ready.write_text("entered")
            if mode == "cleanup":
                # The original entry's finally is still guarded, not only user code.
                try:
                    return {}
                finally:
                    ctypes.CDLL(None).sleep(60)
            return {}

        owner_child.runpy.run_module = payload
        sys.argv = ["owner_child", "--context", str(context)]
        if mode == "refused":
            try:
                owner_child.main()
            except RuntimeError as error:
                assert not ready.exists()
                sys.stdout.write(
                    json.dumps(
                        {
                            "error": str(error),
                            "watchdog_live": any(
                                t.name == "exec-original-deadline" for t in threading.enumerate()
                            ),
                        }
                    )
                    + "\n"
                )
                return
            raise AssertionError("invalid gate reached the payload")
        owner_child.main()
        sys.stdout.write(
            json.dumps(
                {
                    "watchdog_live": any(
                        t.name == "exec-original-deadline" for t in threading.enumerate()
                    )
                }
            )
            + "\n"
        )
    else:
        if mode == "expired-stop":

            def stalled(self: owner_child._DeadlineWatchdog) -> None:
                time.sleep(60)

            owner_child._DeadlineWatchdog._wait_until_deadline = stalled
        with owner_child._DeadlineWatchdog(datetime.now(UTC) + timedelta(seconds=0.1)) as watchdog:
            if mode == "expired-stop":
                # Keep the worker out of arbitration; close must not erase expiry.
                time.sleep(0.15)
                watchdog.close()
            else:
                watchdog.close()
                time.sleep(0.2)
                watchdog.close()
        sys.stdout.write(
            json.dumps(
                {"finished": watchdog._finished.is_set(), "alive": watchdog._thread.is_alive()}
            )
            + "\n"
        )


if __name__ == "__main__":
    mode, context, ready = sys.argv[1], Path(sys.argv[2]), Path(sys.argv[3])
    if mode == "parent":
        _parent(context)
    elif mode == "orphan":
        _orphan(context, ready)
    else:
        _entry(mode, context, ready)
