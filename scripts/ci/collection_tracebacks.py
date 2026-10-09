"""Own periodic, GIL-held stack diagnostics for a bounded collection command."""

import faulthandler
import math
import threading
from collections.abc import Generator
from contextlib import contextmanager
from typing import TextIO


@contextmanager
def periodic_tracebacks(stacks: TextIO, *, interval: float = 60) -> Generator[None]:
    """Dump all threads periodically; stop and join before the caller closes the file.

    Unlike dump_traceback_later's native watchdog, this Python thread holds the
    GIL while faulthandler reads interpreter frames. A native call holding the
    GIL can delay this best-effort diagnostic; the command's external timeout
    remains responsible for terminating a stalled collection.
    """
    if not math.isfinite(interval) or interval <= 0:
        raise ValueError("Traceback interval must be finite and positive")
    stopped = threading.Event()
    failures: list[Exception] = []

    def dump() -> None:
        while not stopped.wait(interval):
            try:
                stacks.write(f"Periodic traceback ({interval:g}s interval):\n")
                stacks.flush()
                faulthandler.dump_traceback(file=stacks, all_threads=True)
            except Exception as error:
                # Transfer a diagnostic failure to the owner, never a successful exit.
                failures.append(error)
                return

    worker = threading.Thread(target=dump, name="collection-tracebacks")
    worker.start()
    try:
        yield
    finally:
        stopped.set()
        worker.join()
        if failures:
            raise RuntimeError("Collection traceback diagnostic failed") from failures[0]
