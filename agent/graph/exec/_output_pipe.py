"""Nonblocking output owned by the invocation that launched its known process.

Loop readiness, polling and the close/reap barrier pump the same descriptor. There is no reader
thread or executor worker to outlive cancellation of the invocation.
"""

import asyncio
import os
import select
import subprocess
import time

from ._stream import StreamingTextIO


class ExecOutputPipe:
    """One merged output pipe; EOF, rather than a timed wait, settles it."""

    def __init__(self, proc: subprocess.Popen[bytes], stream: StreamingTextIO) -> None:
        if proc.stdout is None:
            raise RuntimeError("exec child has no output pipe")
        self.stdout = proc.stdout
        self.descriptor = self.stdout.fileno()
        self.stream = stream
        self.closed = False
        self.error: BaseException | None = None
        self.loop: asyncio.AbstractEventLoop | None = None
        os.set_blocking(self.descriptor, False)

    def watch(self) -> None:
        """The invocation's loop owns readiness; its poll still owns publication."""
        self.loop = asyncio.get_running_loop()
        self.loop.add_reader(self.descriptor, self.read_ready)

    def read_ready(self) -> None:
        """Retain a callback failure for the invocation's poll or tail owner."""
        try:
            self.pump()
        except BaseException as error:
            self.error = error
            self.close()

    def pump(self) -> None:
        """Read at most 256 KiB per beat so output cannot starve cancellation."""
        if self.error is not None:
            raise self.error
        if self.closed:
            return
        try:
            for _ in range(4):
                chunk = os.read(self.descriptor, 65536)
                if not chunk:
                    self.close()
                    return
                self.stream.write(chunk.decode("utf-8", errors="replace"))
        except BlockingIOError:
            return
        except (OSError, ValueError):
            # Match the existing broken-pipe best-effort capture contract.
            self.close()

    def close(self) -> None:
        if not self.closed:
            self.closed = True
            if self.loop is not None:
                self.loop.remove_reader(self.descriptor)
            self.stdout.close()

    async def finish(self, timeout: float) -> None:
        """Pump the bounded tail; a retained writer remains explicitly unresolved."""
        deadline = time.monotonic() + timeout
        self.pump()
        while not self.closed:
            self.pump()
            if self.closed or time.monotonic() >= deadline:
                return
            await asyncio.sleep(min(0.01, max(0, deadline - time.monotonic())))

    def finish_now(self, timeout: float) -> None:
        """Bounded synchronous tail for Runner-wide cancellation of async owners."""
        deadline = time.monotonic() + timeout
        self.pump()
        while not self.closed:
            self.pump()
            if self.closed or time.monotonic() >= deadline:
                return
            select.select([self.stdout], [], [], max(0, deadline - time.monotonic()))
