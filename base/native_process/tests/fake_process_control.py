from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Literal

from base.native_process.process_control import ProcessControl


@dataclass(frozen=True)
class Signalled:
    kind: Literal["kill", "killpg"]
    target: int
    signum: int


class FakeProcessControl:
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
