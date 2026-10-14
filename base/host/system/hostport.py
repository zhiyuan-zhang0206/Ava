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
import platform
import shutil
import sys
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from base.host.proc import CommandRunner, run_bounded

__all__ = ["Host", "Platform"]


@dataclass(frozen=True)
class Platform:
    """The operating-system facts a component branches on."""

    # `sys.platform` reduced to the two supported systems.
    system: Literal["linux", "darwin"]
    # The hardware name as the OS reports it (`platform.machine()`): "arm64", "x86_64", "aarch64".
    machine: str
    # The running interpreter's absolute path (`sys.executable`).
    executable: str

    def __post_init__(self) -> None:
        if self.system not in ("linux", "darwin"):
            raise ValueError(f"unsupported platform system {self.system!r}: linux or darwin")
        if not self.machine:
            raise ValueError("platform machine is empty")
        if not self.executable:
            raise ValueError("platform executable is empty")

    @classmethod
    def from_sys_platform(cls, sys_platform: str, *, machine: str, executable: str) -> Platform:
        """Build from a `sys.platform` value; anything but macOS and Linux is refused."""
        if sys_platform == "darwin":
            return cls("darwin", machine, executable)
        if sys_platform.startswith("linux"):
            return cls("linux", machine, executable)
        raise ValueError(f"unsupported platform {sys_platform!r}: Ava runs on macOS and Linux")

    @classmethod
    def current(cls) -> Platform:
        """The platform of this process."""
        return cls.from_sys_platform(
            sys.platform, machine=platform.machine(), executable=sys.executable
        )


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
