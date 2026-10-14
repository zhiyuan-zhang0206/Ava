"""`Host`: the port to this machine's processes, executables and platform.

A component that starts a process, looks up an executable or branches on the platform takes a
`host: Host` as a required argument and calls `host.run`, `host.which`, `host.platform` and
`host.uid` instead of `subprocess`, `shutil.which`, `sys.platform` and `os.getuid`. The
composition root builds one with `Host.system()`; a test builds the host it wants, with
`FakeCommands` (`base/host/tests/fake_commands.py`) as `run` and plain values for the rest.
A test that really needs a process uses `Host.system()` and patches nothing.

`run` is `base.host.proc.run_bounded`, not a second runner: its timeout bounds the whole process
tree. The four fields are the whole port; another capability gets its own port or a reviewed
addition here, so the object does not become a bag of everything the machine can do.
"""

from __future__ import annotations

import os
import shutil
from collections.abc import Callable
from dataclasses import dataclass

from base.host.proc import CommandRunner, run_bounded
from base.native_process.os_platform import Platform

__all__ = ["Host"]


@dataclass(frozen=True)
class Host:
    """This machine as a component sees it: run a command, find a program, platform, user."""

    # `base.host.proc.run_bounded` in production.
    run: CommandRunner
    # `shutil.which` in production: a program name or path to its resolved path, or None.
    which: Callable[[str], str | None]
    platform: Platform
    # The real user id of the process (`os.getuid()`).
    uid: int

    @classmethod
    def system(cls) -> Host:
        """The real machine; the composition root's constructor."""
        return cls(
            run=run_bounded,
            which=shutil.which,
            platform=Platform.current(),
            uid=os.getuid(),
        )
