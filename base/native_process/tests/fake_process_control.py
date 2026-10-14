"""`FakeProcessControl`: a recording `ProcessControl` for tests of code that signals processes.

It records every signal a component sends, in order, and sends none unless asked to: a signal to
a fake group changes nothing, so a test can show a group that stays live after SIGKILL, a signal
that is denied, or that no signal was sent at all.

    process_control = FakeProcessControl(groups={4242: 4242})
    process_control.refuse(4242, PermissionError("operation not permitted"))
    ...
    assert process_control.signals == [Signalled("killpg", 4242, signal.SIGTERM)]

`forward_to=SYSTEM_PROCESS_CONTROL` makes the fake forward each signal and each read after it
records them, for a test that signals a real child and also wants the order or the refusal.
`on_signal` is called with each signal before it is forwarded or refused, so a test can interleave
the signals with the other events it records.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Literal

from base.native_process.process_control import ProcessControl


@dataclass(frozen=True)
class Signalled:
    """One signal a component sent: to a process (`kill`) or to a group (`killpg`)."""

    kind: Literal["kill", "killpg"]
    target: int
    signum: int


class FakeProcessControl:
    """A `ProcessControl` that records signals and answers group and session reads from a table."""

    def __init__(
        self,
        *,
        groups: Mapping[int, int] | None = None,
        sessions: Mapping[int, int] | None = None,
        forward_to: ProcessControl | None = None,
        on_signal: Callable[[Signalled], None] | None = None,
    ) -> None:
        self.signals: list[Signalled] = []
        self._groups = dict(groups or {})
        self._sessions = dict(sessions or {})
        self._forward_to = forward_to
        self._on_signal = on_signal
        self._refused: dict[int, OSError] = {}

    def refuse(self, target: int, error: OSError) -> None:
        """Make every signal to `target` (a pid or a group id) raise `error`, after recording it."""
        self._refused[target] = error

    def kill(self, pid: int, signum: int, /) -> None:
        self._signal(Signalled("kill", pid, signum))
        if self._forward_to is not None:
            self._forward_to.kill(pid, signum)

    def killpg(self, pgid: int, signum: int, /) -> None:
        self._signal(Signalled("killpg", pgid, signum))
        if self._forward_to is not None:
            self._forward_to.killpg(pgid, signum)

    def getpgid(self, pid: int, /) -> int:
        if pid in self._groups:
            return self._groups[pid]
        if self._forward_to is not None:
            return self._forward_to.getpgid(pid)
        raise ProcessLookupError(pid)

    def getsid(self, pid: int, /) -> int:
        if pid in self._sessions:
            return self._sessions[pid]
        if self._forward_to is not None:
            return self._forward_to.getsid(pid)
        raise ProcessLookupError(pid)

    def _signal(self, sent: Signalled) -> None:
        self.signals.append(sent)
        if self._on_signal is not None:
            self._on_signal(sent)
        if sent.target in self._refused:
            raise self._refused[sent.target]
