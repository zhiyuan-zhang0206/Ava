"""Fixed child entry: user code cannot start before the owner's exact permit.

Only the independent owner holds this pipe's write end. The payload receives
neither that handle nor the original host's control writer. EOF is refusal.
"""

import argparse
import hashlib
import os
import runpy
import sys
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from types import TracebackType

from base.agents.incarnation.exec_owner_protocol import (
    MAX_OWNER_MESSAGE,
    OwnerContext,
    OwnerControl,
    read_owner_bytes,
    read_owner_context,
)

_WATCHDOG_JOIN_S = 5.0


class _DeadlineWatchdog:
    """The gated child's independent deadline, owned through its entire entry.

    The daemon can exit native code that releases the GIL even after its parent
    disappears. Normal stop disarms only a deadline that has not yet expired.
    """

    def __init__(self, deadline: datetime) -> None:
        # Sampling monotonic first never adds setup time to the allocation.
        monotonic_now = time.monotonic()
        remaining = (deadline - datetime.now(UTC)).total_seconds()
        if remaining <= 0:
            raise RuntimeError("exec deadline expired before owner permit")
        self._deadline = monotonic_now + remaining
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._finished = threading.Event()
        self._error: BaseException | None = None
        self._thread = threading.Thread(
            target=self._run, daemon=True, name="exec-original-deadline"
        )
        self._thread.start()

    def _wait_until_deadline(self) -> None:
        if not self._stop.wait(max(0, self._deadline - time.monotonic())):
            with self._lock:
                if not self._stop.is_set():
                    os._exit(124)

    def _run(self) -> None:
        try:
            self._wait_until_deadline()
        except BaseException as error:
            self._error = error
            # Use Python's existing stderr error boundary before any SDK boot.
            # The parent relays this output; close still raises the original.
            sys.excepthook(type(error), error, error.__traceback__)
        finally:
            self._finished.set()

    def close(self) -> None:
        """Stop further deadline decisions, then collect the actual worker result."""
        with self._lock:
            if not self._stop.is_set() and time.monotonic() >= self._deadline:
                os._exit(124)
            self._stop.set()
        self._thread.join(timeout=_WATCHDOG_JOIN_S)
        unfinished = self._thread.is_alive()
        if self._error is not None:
            if unfinished:
                self._error.add_note("exec deadline watchdog join remains unfinished")
            raise self._error
        if unfinished or not self._finished.is_set():
            raise RuntimeError("exec deadline watchdog join remains unfinished")

    def __enter__(self) -> "_DeadlineWatchdog":
        return self

    def __exit__(
        self,
        _kind: type[BaseException] | None,
        primary: BaseException | None,
        _traceback: TracebackType | None,
    ) -> None:
        try:
            self.close()
        except BaseException as cleanup:
            if primary is None:
                raise
            primary.add_note(f"exec deadline watchdog cleanup also failed: {cleanup!r}")
            secondary = [cleanup]
            if primary.__cause__ is not None:
                secondary.insert(0, primary.__cause__)
            raise primary from BaseExceptionGroup("exec deadline cleanup failures", secondary)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--context", type=Path, required=True)
    args = parser.parse_args()
    context = read_owner_context(args.context)
    with _DeadlineWatchdog(context.allocation.deadline):
        _run_permitted(context)


def _run_permitted(context: OwnerContext) -> None:
    raw = sys.stdin.buffer.readline(MAX_OWNER_MESSAGE + 1)
    if not raw or len(raw) > MAX_OWNER_MESSAGE:
        raise RuntimeError("exec owner permit pipe closed or exceeded its bound")
    control = OwnerControl.model_validate_json(raw)
    if (control.request, control.domain, control.action) != (
        context.allocation.request,
        context.allocation.domain,
        "permit",
    ):
        raise RuntimeError("exec owner permit differs from the exact allocation")
    if datetime.now(UTC) >= context.allocation.deadline:
        raise RuntimeError("exec deadline expired while waiting for owner permit")
    if (
        Path(os.environ["AVA_EXEC_REQUEST_FILE"]) != context.request_path
        or Path(os.environ["AVA_EXEC_RESULT_FILE"]) != context.result_path
        or hashlib.sha256(
            # Defensive ceiling for hashing the launcher-written request
            # envelope: far above any legitimate request, still a bounded read
            # (task #3696 exception inventory).
            read_owner_bytes(context.request_path, limit=64 * 1024 * 1024)
        ).hexdigest()
        != context.allocation.request_digest
    ):
        raise RuntimeError("exec payload paths or bytes changed after allocation")
    with Path(os.devnull).open("rb") as empty:
        os.dup2(empty.fileno(), 0)
    # The actual old child entry, not a second execution engine. Its request
    # and result environment is prepared by the original runtime as before.
    runpy.run_module("agent.execution.child", run_name="__main__")


if __name__ == "__main__":
    main()
