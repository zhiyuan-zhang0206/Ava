"""A deterministic page shell backend used by actual reconcile operations."""

from pathlib import Path

__all__ = ["PageShellBackend"]


class PageShellBackend:
    def __init__(self) -> None:
        self.sessions: set[str] = set()
        self.new_calls: list[tuple[str, str, Path, dict[str, str]]] = []
        self.sent: list[tuple[str, str]] = []
        self.keys: list[tuple[str, tuple[str, ...]]] = []
        self.killed: list[str] = []
        self.new_result = True
        self.supports_send = True
        self.send_error: Exception | None = None

    def has_session(self, name: str) -> bool:
        return name in self.sessions

    def new_session(
        self, name: str, command: str, cwd: Path, *, env: dict[str, str], **_kwargs: object
    ) -> bool:
        self.new_calls.append((name, command, cwd, env))
        if self.new_result:
            self.sessions.add(name)
        return self.new_result

    def send(self, name: str, text: str) -> None:
        if not self.supports_send:
            raise NotImplementedError
        if self.send_error is not None:
            raise self.send_error
        self.sent.append((name, text))

    def send_keys(self, name: str, *keys: str) -> None:
        self.keys.append((name, keys))

    def kill_session(self, name: str, **_kwargs: object) -> tuple[bool, str]:
        self.killed.append(name)
        self.sessions.discard(name)
        return True, "forced"
