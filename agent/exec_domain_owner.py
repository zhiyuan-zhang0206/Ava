"""Short-lived direct parent of one managed exec domain; never a service.

The original runtime alone owns stdin's write end. EOF requests closure, not
success. The terminal receipt is published only after native member closure,
root reap and output EOF. Independent persistent sessions are outside this domain.
"""

import argparse
import hashlib
import queue
import subprocess
import sys
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

import psutil

from agent.graph._exec_process import ExecProcessDomain
from agent.graph._exec_protocol import KILL_GRACE_S
from shared.exec_owner_protocol import (
    MAX_OWNER_MESSAGE,
    OwnerClosed,
    OwnerControl,
    OwnerReady,
    publish_owner_message,
    read_owner_bytes,
    read_owner_context,
)
from shared.incarnation_resources import ResourceProcess
from shared.platform import CREATE_NO_WINDOW, IS_WINDOWS
from shared.winjob import WindowsJob


def _ended(identity: psutil.Process) -> bool:
    try:
        return identity.status() in {psutil.STATUS_DEAD, psutil.STATUS_ZOMBIE}
    except psutil.NoSuchProcess:
        return True


def _control(messages: queue.Queue[bytes]) -> None:
    while True:
        value = sys.stdin.buffer.readline(MAX_OWNER_MESSAGE + 1)
        messages.put(value)
        if not value or len(value) > MAX_OWNER_MESSAGE:
            return


def _relay(root: subprocess.Popen[bytes], failures: list[BaseException]) -> None:
    if root.stdout is None:
        failures.append(RuntimeError("owner root has no output pipe"))
        return
    destination = sys.stdout.buffer
    writable = True
    try:
        while chunk := root.stdout.read(8192):
            if writable:
                try:
                    destination.write(chunk)
                    destination.flush()
                except (BrokenPipeError, OSError):
                    # Host death must not leave the root's output pipe undrained.
                    writable = False
    except BaseException as exc:
        failures.append(exc)
    finally:
        root.stdout.close()


def run(context_path: Path) -> None:  # noqa: PLR0915 -- one native owner retains cleanup ordering.
    context = read_owner_context(context_path)
    allocation = context.allocation
    remaining = (allocation.deadline - datetime.now(UTC)).total_seconds()
    if remaining <= 0:
        raise RuntimeError("exec owner allocation expired before spawn")
    deadline = time.monotonic() + remaining
    close_deadline = deadline + KILL_GRACE_S
    if (
        hashlib.sha256(read_owner_bytes(context.request_path, 64 * 1024 * 1024)).hexdigest()
        != allocation.request_digest
    ):
        raise RuntimeError("exec owner request digest differs from reservation")
    messages: queue.Queue[bytes] = queue.Queue(maxsize=2)
    threading.Thread(target=_control, args=(messages,), daemon=True).start()
    job = WindowsJob.create() if IS_WINDOWS else None
    try:
        root = subprocess.Popen(  # noqa: S603 -- fixed isolated entry; no caller-selected executable.
            [
                sys.executable,
                "-I",
                "-X",
                "utf8",
                "-m",
                "agent.exec_owner_child",
                "--context",
                str(context_path),
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            close_fds=True,
            creationflags=CREATE_NO_WINDOW,
            start_new_session=not IS_WINDOWS,
        )
    except BaseException:
        if job is not None:
            job.close()
        raise
    domain = ExecProcessDomain(root, job)
    reader_failures: list[BaseException] = []
    reader = threading.Thread(target=_relay, args=(root, reader_failures), daemon=True)
    reason: Literal["completed", "host_eof", "cancel", "timeout"] = "completed"
    attached = False
    try:
        if job is not None:
            job.assign(root)
        attached = True
        root_identity = psutil.Process(root.pid)
        owner_identity = psutil.Process()
        allocation = allocation.model_copy(
            update={
                "owner_process": ResourceProcess(
                    pid=owner_identity.pid, birth=owner_identity.create_time()
                ),
                "root_process": ResourceProcess(pid=root.pid, birth=root_identity.create_time()),
            }
        )
        reader.start()
        publish_owner_message(context_path.with_suffix(".ready"), OwnerReady(allocation=allocation))
        permitted = False
        while not _ended(root_identity):
            if time.monotonic() >= deadline:
                reason = "timeout"
                break
            try:
                raw = messages.get(timeout=min(0.05, max(0.001, deadline - time.monotonic())))
            except queue.Empty:
                continue
            if not raw:
                reason = "host_eof"
                break
            message = OwnerControl.model_validate_json(raw)
            if (message.request, message.domain) != (allocation.request, allocation.domain):
                raise RuntimeError("owner control belongs to another allocation")  # noqa: TRY301 -- same owned cleanup boundary.
            if message.action == "cancel":
                reason = "cancel"
                break
            if permitted:
                raise RuntimeError("exec permit cannot be replayed")  # noqa: TRY301 -- same owned cleanup boundary.
            if root.stdin is None:
                raise RuntimeError("owner root has no permit pipe")  # noqa: TRY301 -- cleanup must still run.
            root.stdin.write(raw)
            root.stdin.flush()
            root.stdin.close()
            permitted = True
        domain.close_confirmed(close_deadline)
        code = root.wait(timeout=max(0.001, close_deadline - time.monotonic()))
        reader.join(timeout=max(0, close_deadline - time.monotonic()))
        if reader.is_alive() or reader_failures:
            raise RuntimeError("exec owner output barrier is unresolved")  # noqa: TRY301 -- do not publish on cleanup uncertainty.
        publish_owner_message(
            context_path.with_suffix(".closed"),
            OwnerClosed(
                allocation=allocation,
                root_exit_code=code,
                reason=reason,
                observed_at=datetime.now(UTC),
            ),
        )
    except BaseException as original:
        try:
            if attached:
                domain.close_confirmed(close_deadline)
            else:
                root.kill()
            root.wait(timeout=max(0.001, close_deadline - time.monotonic()))
        except BaseException as cleanup:
            original.add_note(f"owner cleanup unresolved: {type(cleanup).__name__}: {cleanup}")
        raise
    finally:
        # Never turn failed cleanup into a terminal receipt. Closing the native
        # handle on error is best effort containment, not positive evidence.
        if job is not None and not job.closed:
            job.close()
        if not attached:
            root.kill()
        if root.stdin is not None and not root.stdin.closed:
            root.stdin.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--context", type=Path, required=True)
    run(parser.parse_args().context)


if __name__ == "__main__":
    main()
