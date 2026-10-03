"""Helpers of the pty-sessions service tests: real shells, driven through the client."""

from __future__ import annotations

import time
from collections.abc import Callable
from pathlib import Path

import psutil

from base.sessions.pty import client
from base.sessions.pty.keys import keys_to_bytes

# A ceiling, not a delay: every wait returns the moment its condition holds. Under CI
# CPU oversubscription a reader thread can lag a reap well past 10 s.
WAIT_S = 30.0


def wait_for(
    predicate: Callable[[], bool], timeout: float = WAIT_S, interval: float = 0.05
) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


def new(name: str, cwd: Path, env: dict[str, str] | None = None, cmd: str | None = None) -> bool:
    return client.create_session(name, str(cwd), env or {}, cmd)


def type_line(name: str, text: str) -> None:
    """Type `text` and submit it."""
    client.send(name, text.encode() + b"\r")


def press(name: str, *keys: str) -> None:
    client.send(name, keys_to_bytes(keys))


def screen(name: str, *, scrollback: bool = True) -> str:
    return client.capture(name, 200, scrollback=scrollback)


def output_until(name: str, word: str, *, timeout: float = WAIT_S) -> str:
    """Wait until a BARE output line equal to `word` appears in the capture.

    The typed command line is echoed onto the screen, so a substring wait is
    satisfied by the echo before the command has run. Waiting for the bare
    line (the command's own output) pins the shell to "command executed".
    """

    def seen() -> bool:
        return word in [line.strip() for line in screen(name).split("\n")]

    assert wait_for(seen, timeout), f"output line {word!r} never appeared:\n{screen(name)}"
    return screen(name)


def shell_process(name: str) -> psutil.Process:
    (info,) = [s for s in client.list_sessions() if s.name == name]
    return psutil.Process(info.pid)


def gone(process: psutil.Process) -> bool:
    """True once the process can no longer execute: reaped, or a zombie awaiting reap."""
    try:
        return process.status() == psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:
        return True
