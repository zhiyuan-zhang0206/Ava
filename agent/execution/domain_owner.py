"""Short-lived direct parent of one managed exec domain; never a service.

The original runtime alone owns stdin's write end. EOF requests closure, not
success. The terminal receipt is published only after native member closure,
root reap and output EOF. Independent persistent sessions are outside this domain.
"""

import argparse
import hashlib
import os
import subprocess
import sys
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

import psutil

from base.agents.incarnation.exec_owner_protocol import (
    MAX_OWNER_MESSAGE,
    OwnerClosed,
    OwnerControl,
    OwnerReady,
    publish_owner_message,
    read_owner_bytes,
    read_owner_context,
)
from base.agents.incarnation.resources import ExecAllocation, ResourceProcess
from base.native_process.exec_domain import KILL_GRACE_S, ExecProcessDomain


def _ended(identity: psutil.Process) -> bool:
    try:
        return identity.status() in {psutil.STATUS_DEAD, psutil.STATUS_ZOMBIE}
    except psutil.NoSuchProcess:
        return True


class ControlPipe:
    """The owner loop alone reads control; no buffered daemon survives shutdown.

    Python 3.12 supports nonblocking pipes. Partial records
    retain their fixed bound, and EOF never upgrades a truncated record to permit.
    """

    def __init__(self, descriptor: int) -> None:
        self.descriptor = descriptor
        self.pending = b""
        os.set_blocking(descriptor, False)

    def read(self) -> bytes | None:
        if b"\n" not in self.pending:
            try:
                chunk = os.read(self.descriptor, MAX_OWNER_MESSAGE + 1 - len(self.pending))
            except BlockingIOError:
                return None
            if not chunk:
                if self.pending:
                    raise RuntimeError("owner control pipe ended in a partial record")
                return b""
            self.pending += chunk
        line, separator, remainder = self.pending.partition(b"\n")
        if len(line) + len(separator) > MAX_OWNER_MESSAGE:
            raise RuntimeError("owner control record exceeds its bound")
        if not separator:
            return None
        self.pending = remainder
        return line + separator


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


_Reason = Literal["completed", "host_eof", "cancel", "timeout"]


def _deadlines(allocation: ExecAllocation) -> tuple[float, float]:
    """`(exec deadline, close deadline)` on the monotonic clock; refuses an expired allocation."""
    remaining = (allocation.deadline - datetime.now(UTC)).total_seconds()
    if remaining <= 0:
        raise RuntimeError("exec owner allocation expired before spawn")
    deadline = time.monotonic() + remaining
    return deadline, deadline + KILL_GRACE_S


def _forward_permit(root: subprocess.Popen[bytes], raw: bytes) -> None:
    if root.stdin is None:
        raise RuntimeError("owner root has no permit pipe")
    root.stdin.write(raw)
    root.stdin.flush()
    root.stdin.close()


def _control_loop(
    root: subprocess.Popen[bytes],
    root_identity: psutil.Process,
    control: ControlPipe,
    allocation: ExecAllocation,
    deadline: float,
) -> _Reason:
    """Relay the one permit and watch for cancel / host EOF / deadline until the root ends."""
    permitted = False
    # Keep the root unreaped to pin its process group.
    while not _ended(root_identity):
        if time.monotonic() >= deadline:
            return "timeout"
        raw = control.read()
        if raw is None:
            time.sleep(min(0.05, max(0.001, deadline - time.monotonic())))
            continue
        if not raw:
            return "host_eof"
        message = OwnerControl.model_validate_json(raw)
        if (message.request, message.domain) != (allocation.request, allocation.domain):
            raise RuntimeError("owner control belongs to another allocation")
        if message.action == "cancel":
            return "cancel"
        if permitted:
            raise RuntimeError("exec permit cannot be replayed")
        _forward_permit(root, raw)
        permitted = True
    return "completed"


def _close_after_failure(
    original: BaseException,
    root: subprocess.Popen[bytes],
    domain: ExecProcessDomain,
    close_deadline: float,
    *,
    attached: bool,
    close_attempted: bool,
) -> None:
    """Best-effort closure of a failed owner; unresolved cleanup is recorded on `original`."""
    try:
        if attached and not close_attempted:
            domain.close_confirmed(close_deadline)
            root.wait(timeout=max(0.001, close_deadline - time.monotonic()))
        elif not attached:
            root.kill()
            root.wait(timeout=max(0.001, close_deadline - time.monotonic()))
    except BaseException as cleanup:
        original.add_note(f"owner cleanup unresolved: {type(cleanup).__name__}: {cleanup}")


def run(context_path: Path) -> None:
    context = read_owner_context(context_path)
    allocation = context.allocation
    deadline, close_deadline = _deadlines(allocation)
    if (
        hashlib.sha256(read_owner_bytes(context.request_path, 64 * 1024 * 1024)).hexdigest()
        != allocation.request_digest
    ):
        raise RuntimeError("exec owner request digest differs from reservation")
    control = ControlPipe(sys.stdin.fileno())
    argv = [
        sys.executable,
        "-I",
        "-B",
        "-X",
        "utf8",
        "-m",
        "agent.execution.owner_child",
        "--context",
        str(context_path),
    ]
    root, domain = ExecProcessDomain.launch_posix(
        argv,
        new_session=True,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        close_fds=True,
        bufsize=0,
    )
    reader_failures: list[BaseException] = []
    reader = threading.Thread(target=_relay, args=(root, reader_failures), daemon=True)
    attached = False
    close_attempted = False
    try:
        attached = True
        root_identity = psutil.Process(root.pid)
        owner_identity = psutil.Process()
        allocation = allocation.model_copy(
            update={
                "owner_process": ResourceProcess.capture(owner_identity),
                "root_process": ResourceProcess.capture(root_identity),
            }
        )
        reader.start()
        publish_owner_message(context_path.with_suffix(".ready"), OwnerReady(allocation=allocation))
        reason = _control_loop(root, root_identity, control, allocation, deadline)
        close_attempted = True
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
        _close_after_failure(
            original,
            root,
            domain,
            close_deadline,
            attached=attached,
            close_attempted=close_attempted,
        )
        raise
    finally:
        # Never turn failed cleanup into a terminal receipt.
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
