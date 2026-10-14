"""`FakeCommands`: a scripted `CommandRunner` for tests of code that takes a `Host`.

It records every call and answers from a script, shaped like `base.host.proc.run_bounded`: the
result is a `CompletedProcess` whose streams are `None` unless the caller captured them, and
text or bytes according to the caller's `text=` / `encoding=`. A command nobody scripted fails
the test instead of answering with a default, so a test never passes by accident.

    commands = FakeCommands()
    commands.on("lsof", Reply(stdout="p1109\\n"))
    host = Host(run=commands, which=lambda name: None, platform=..., uid=1000)
    ...
    assert commands.argvs == [("lsof", "-nP")]
"""

from __future__ import annotations

import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Reply:
    """What a scripted command prints and how it exits."""

    stdout: str | bytes = ""
    stderr: str | bytes = ""
    returncode: int = 0


@dataclass(frozen=True)
class Call:
    """One command a component ran: its argv, the timeout it asked for and its other keywords."""

    argv: tuple[str, ...]
    timeout: float
    kwargs: Mapping[str, Any]


@dataclass
class _Route:
    prefix: tuple[str, ...]
    replies: list[Reply | BaseException]
    repeat: bool


def _shape(value: str | bytes, *, text: bool) -> str | bytes:
    if text:
        return value.decode() if isinstance(value, bytes) else value
    return value if isinstance(value, bytes) else value.encode()


def _is_piped(capture_output: bool, popen_kwargs: Mapping[str, object], stream: str) -> bool:
    return capture_output or popen_kwargs.get(stream) == subprocess.PIPE


def _result(
    command: tuple[str, ...],
    reply: Reply,
    *,
    capture_output: bool,
    popen_kwargs: Mapping[str, object],
) -> subprocess.CompletedProcess[Any]:
    """The `CompletedProcess` `run_bounded` would return for this reply and these keywords."""
    text = bool(popen_kwargs.get("text") or popen_kwargs.get("encoding"))
    out = err = None
    if _is_piped(capture_output, popen_kwargs, "stdout"):
        out = _shape(reply.stdout, text=text)
    if _is_piped(capture_output, popen_kwargs, "stderr"):
        err = _shape(reply.stderr, text=text)
    return subprocess.CompletedProcess(list(command), reply.returncode, out, err)


class FakeCommands:
    """A `CommandRunner` that records calls and replays a script routed by argv prefix."""

    def __init__(self) -> None:
        self.calls: list[Call] = []
        self._routes: list[_Route] = []

    @property
    def argvs(self) -> list[tuple[str, ...]]:
        """The argv of every call, in order."""
        return [call.argv for call in self.calls]

    def on(
        self,
        prefix: str | Sequence[str],
        *replies: Reply | BaseException,
        repeat: bool = False,
    ) -> None:
        """Script the commands whose argv starts with `prefix` (a program name or a list).

        Each matching call consumes the next reply: a `Reply` is returned, an exception is
        raised (`subprocess.TimeoutExpired`, `FileNotFoundError`). With `repeat=True` the last
        reply answers every call after the script runs out. The first route that matches wins,
        so register the more specific prefix first; an empty prefix matches everything.
        """
        if not replies:
            raise ValueError("a route needs at least one reply")
        key = (prefix,) if isinstance(prefix, str) else tuple(prefix)
        self._routes.append(_Route(key, list(replies), repeat))

    def _next_reply(self, command: tuple[str, ...]) -> Reply | BaseException:
        route = next((r for r in self._routes if command[: len(r.prefix)] == r.prefix), None)
        if route is None:
            raise AssertionError(f"unscripted command: {list(command)}")
        if not route.replies:
            raise AssertionError(f"replies for {list(route.prefix)} ran out: {list(command)}")
        keep_last = route.repeat and len(route.replies) == 1
        return route.replies[0] if keep_last else route.replies.pop(0)

    def __call__(
        self,
        argv: Sequence[str],
        *,
        timeout: float,
        capture_output: bool = False,
        input: bytes | str | None = None,  # mirrors subprocess.run's own parameter name
        **popen_kwargs: object,
    ) -> subprocess.CompletedProcess[Any]:
        command = tuple(argv)
        kwargs: dict[str, Any] = {"capture_output": capture_output, "input": input, **popen_kwargs}
        self.calls.append(Call(command, timeout, kwargs))
        reply = self._next_reply(command)
        if isinstance(reply, BaseException):
            raise reply
        return _result(command, reply, capture_output=capture_output, popen_kwargs=popen_kwargs)
