"""Terminal-only facade; ordinary agent execution never enters this resource API."""

from __future__ import annotations

from pathlib import Path

from shared.root_control.client import RootClient
from shared.root_control.ipc import encode, parse_response
from shared.root_control.windows.transport import roundtrip
from shared.session_backend import SessionBackend
from shared.windows_terminal.record import TerminalRecord, endpoint, read, record_path


def query(
    record: TerminalRecord, *, close: bool = False, force: bool = False, timeout: float = 2.0
) -> TerminalRecord:
    """Bind the reply to the connected native owner and unchanged domain."""
    owner = record.owner
    if owner is None or not owner.identity().live():
        raise RuntimeError("terminal owner unavailable; custody requires reconciliation")
    timeout = min(timeout, 25.0)
    raw, peer = roundtrip(
        endpoint(record),
        encode(
            {
                "domain": record.domain,
                "verb": "close" if close else "status",
                "force": force,
                "timeout": timeout,
            }
        ),
        timeout + 1.0,
    )
    response = parse_response(raw)
    if not response["ok"]:
        raise RuntimeError(response.get("error"))
    observed = TerminalRecord.model_validate(response.get("result"))
    if peer != owner.pid or observed.owner != owner or observed.domain != record.domain:
        raise RuntimeError("terminal control reply does not match its recorded native owner")
    if observed.name != record.name or read(record.name) != observed:
        raise RuntimeError("terminal control reply does not match durable custody")
    return observed


class WindowsTerminalBackend(SessionBackend):
    """A terminal survives root stop; full stop requires its own closure receipt."""

    def has_session(self, name: str) -> bool:
        record = read(name)
        if record is None or record.state == "closed":
            return False
        return query(record).state != "closed"

    def new_session(
        self,
        name: str,
        cmd: str,
        cwd: Path,
        *,
        env: dict[str, str],
        login_shell: bool = True,
        exec_cmd: bool = True,
        gate_fd: int | None = None,
        receipt: tuple[Path, str] | None = None,
    ) -> bool:
        del login_shell, exec_cmd
        if gate_fd is not None or receipt is not None:
            raise NotImplementedError("Windows terminal resources have no POSIX spawn gate")
        record_path(name)  # validate before sending anything
        from shared.paths import root_run_dir

        response = RootClient(root_run_dir() / "ava-root.sock").resource(
            "terminal.start",
            {
                "name": name,
                "command": cmd,
                "cwd": str(cwd.resolve()),
                "env": env,
            },
        )
        if not response["ok"]:
            raise RuntimeError(response.get("error"))
        record = TerminalRecord.model_validate(response.get("result"))
        if record.name != name or record.command != cmd or record.cwd != str(cwd.resolve()):
            raise RuntimeError("root returned a different terminal request")
        if record.state == "closed":
            return True  # a short command completed with native closure proved
        return query(record).state in {"running", "closed"}

    def kill_session(
        self, name: str, *, graceful: bool = False, timeout: float = 15.0, expected: bool = False
    ) -> tuple[bool, str]:
        del expected
        record = read(name)
        if record is None or record.state == "closed":
            return True, "noop"
        try:
            closed = query(record, close=True, force=not graceful, timeout=timeout)
        except (OSError, RuntimeError, ValueError):
            # The owner can exit after durable closure but before its response is
            # delivered. Only that same domain's receipt resolves the ambiguity.
            closed = read(name)
            if closed is None or closed.domain != record.domain or closed.state != "closed":
                raise
        return closed.state == "closed", "graceful" if graceful else "forced"

    def kill_session_with_verdict(
        self, name: str, *, graceful: bool = False, timeout: float = 15.0, expected: bool = False
    ) -> tuple[bool, str, bool]:
        record = read(name)
        busy = record is not None and record.state != "closed"
        ok, mode = self.kill_session(name, graceful=graceful, timeout=timeout, expected=expected)
        return ok, mode, busy

    def list_sessions(self, prefix: str = "") -> list[str]:
        directory = record_path("index").parent
        names: list[str] = []
        for path in directory.glob("*.json"):
            record = read(path.stem)
            if record is not None and record.state != "closed" and record.name.startswith(prefix):
                names.append(record.name)
        return sorted(names)

    def session_generation(self, name: str) -> str | None:
        record = read(name)
        return record.generation if record is not None and record.state != "closed" else None

    def session_started_at(self, name: str) -> float | None:
        record = read(name)
        return record.started_at if record is not None and record.state != "closed" else None

    def session_log_path(self, name: str) -> Path:
        from shared.paths import logs_dir

        record_path(name)
        return logs_dir() / f"{name}.out.log"
