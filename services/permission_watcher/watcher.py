"""Watch macOS TCC/ALF prompts and record their lifecycle locally.

Two unified-log streams feed one queue. Reader threads own only their stream
parser; the main thread exclusively owns persistent pending state and logging,
so no permission incident is mutated concurrently.
"""

from __future__ import annotations

import json
import logging
import queue
import signal
import subprocess
import sys
import threading
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from services.permission_watcher.events import (
    EventPhase,
    LogEventParser,
    PermissionEvent,
    PermissionKind,
    _parse_timestamp,
    log_stream_commands,
)

_LOGGER = logging.getLogger("ava.permission_watcher")

ESCALATION_AFTER = timedelta(minutes=30)
STATE_PATH = Path.home() / ".ava" / "state" / "permission-watcher.json"


@dataclass
class PendingPermission:
    kind: PermissionKind
    subject: str
    tool: str | None
    first_seen: datetime
    last_seen: datetime
    correlation_id: str | None
    escalated: bool = False

    def to_json(self) -> dict[str, object]:
        payload = asdict(self)
        payload["kind"] = self.kind.value
        payload["first_seen"] = self.first_seen.isoformat()
        payload["last_seen"] = self.last_seen.isoformat()
        return payload

    @classmethod
    def from_json(cls, payload: dict[str, object]) -> PendingPermission:
        return cls(
            kind=PermissionKind(str(payload["kind"])),
            subject=str(payload["subject"]),
            tool=str(payload["tool"]) if payload.get("tool") is not None else None,
            first_seen=_parse_timestamp(payload["first_seen"]),
            last_seen=_parse_timestamp(payload["last_seen"]),
            correlation_id=(
                str(payload["correlation_id"])
                if payload.get("correlation_id") is not None
                else None
            ),
            escalated=bool(payload["escalated"]),
        )


class PermissionWatcher:
    """Own persistent pending incidents and log each lifecycle transition once."""

    def __init__(self, state_path: Path) -> None:
        self._state_path = state_path
        self.pending = self._load()

    @staticmethod
    def _key(kind: PermissionKind, subject: str) -> str:
        return f"{kind.value}:{subject}"

    def _load(self) -> dict[str, PendingPermission]:
        if not self._state_path.exists():
            return {}
        raw_payload = json.loads(self._state_path.read_text())
        if not isinstance(raw_payload, dict):
            raise TypeError("permission watcher state is not an object")
        payload = cast(dict[str, object], raw_payload)
        pending = payload["pending"]
        if not isinstance(pending, dict):
            raise TypeError("permission watcher state pending field is not an object")
        result: dict[str, PendingPermission] = {}
        for key, value in cast(dict[object, object], pending).items():
            if not isinstance(key, str) or not isinstance(value, dict):
                raise TypeError("permission watcher pending entry is malformed")
            result[key] = PendingPermission.from_json(cast(dict[str, object], value))
        return result

    def _save(self) -> None:
        self._state_path.parent.mkdir(parents=True, exist_ok=True)
        temp = self._state_path.with_suffix(".tmp")
        temp.write_text(
            json.dumps(
                {
                    "version": 1,
                    "pending": {key: value.to_json() for key, value in self.pending.items()},
                },
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            + "\n"
        )
        temp.chmod(0o600)
        temp.replace(self._state_path)

    def observe(self, event: PermissionEvent) -> None:
        if event.phase is EventPhase.ATTRIBUTION:
            return
        key = self._key(event.kind, event.subject)
        if event.phase is EventPhase.PROMPTING:
            existing = self.pending.get(key)
            if existing is not None:
                existing.last_seen = event.occurred_at
                existing.correlation_id = event.correlation_id or existing.correlation_id
                self._save()
                _LOGGER.debug(
                    "permission prompt repeated: kind=%s subject=%s tool=%s",
                    existing.kind.value,
                    existing.subject,
                    existing.tool,
                )
                return
            pending = PendingPermission(
                kind=event.kind,
                subject=event.subject,
                tool=event.tool,
                first_seen=event.occurred_at,
                last_seen=event.occurred_at,
                correlation_id=event.correlation_id,
            )
            self.pending[key] = pending
            self._save()
            _LOGGER.info(
                "permission prompt: kind=%s subject=%s tool=%s",
                pending.kind.value,
                pending.subject,
                pending.tool,
            )
            return
        pending = self._find_pending(event)
        if pending is None:
            _LOGGER.debug(
                "permission prompt resolution without pending incident: kind=%s subject=%s",
                event.kind.value,
                event.subject,
            )
            return
        _LOGGER.info(
            "permission prompt resolved: kind=%s subject=%s",
            pending.kind.value,
            pending.subject,
        )
        self.pending.pop(self._key(pending.kind, pending.subject), None)
        self._save()

    def _find_pending(self, event: PermissionEvent) -> PendingPermission | None:
        exact = self.pending.get(self._key(event.kind, event.subject))
        if exact is not None:
            return exact
        if event.correlation_id is not None:
            correlated = next(
                (
                    pending
                    for pending in self.pending.values()
                    if pending.kind is event.kind and pending.correlation_id == event.correlation_id
                ),
                None,
            )
            if correlated is not None:
                return correlated
        same_kind = [pending for pending in self.pending.values() if pending.kind is event.kind]
        return same_kind[0] if len(same_kind) == 1 else None

    def check_timeouts(self, now: datetime) -> None:
        changed = False
        for pending in self.pending.values():
            if pending.escalated or now - pending.first_seen < ESCALATION_AFTER:
                continue
            _LOGGER.warning(
                "permission prompt still pending 30min: kind=%s subject=%s",
                pending.kind.value,
                pending.subject,
            )
            pending.escalated = True
            changed = True
        if changed:
            self._save()


@dataclass(frozen=True)
class _StreamFailure:
    kind: PermissionKind
    returncode: int


QueueItem = PermissionEvent | _StreamFailure


def _read_stream(
    kind: PermissionKind,
    process: subprocess.Popen[str],
    items: queue.Queue[QueueItem],
    stopping: threading.Event,
) -> None:
    parser = LogEventParser()
    if process.stdout is None:
        raise RuntimeError(f"{kind.value} log stream has no stdout pipe")
    for line in process.stdout:
        event = parser.parse(kind, line)
        if event is not None:
            items.put(event)
    returncode = process.wait()
    if not stopping.is_set():
        items.put(_StreamFailure(kind, returncode))


def _stop_process(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()


def run_forever(service: PermissionWatcher) -> None:
    """Run both log subscriptions until interrupted or either stream dies."""
    items: queue.Queue[QueueItem] = queue.Queue()
    stopping = threading.Event()
    processes: list[subprocess.Popen[str]] = []
    threads: list[threading.Thread] = []
    try:
        for kind, command in zip(PermissionKind, log_stream_commands(), strict=True):
            process = subprocess.Popen(  # noqa: S603 — fixed /usr/bin/log command and predicates
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )
            processes.append(process)
            thread = threading.Thread(
                target=_read_stream,
                args=(kind, process, items, stopping),
                daemon=True,
                name=f"permission-{kind.value.lower()}-log",
            )
            thread.start()
            threads.append(thread)
        while True:
            try:
                item = items.get(timeout=1)
            except queue.Empty:
                service.check_timeouts(datetime.now(UTC))
                continue
            if isinstance(item, _StreamFailure):
                raise ChildProcessError(
                    f"{item.kind.value} log stream exited with status {item.returncode}"
                )
            service.observe(item)
            service.check_timeouts(datetime.now(UTC))
    finally:
        stopping.set()
        for process in processes:
            _stop_process(process)
        for thread in threads:
            thread.join(timeout=5)


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    def stop(_signum: int, _frame: object) -> None:
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, stop)
    service = PermissionWatcher(STATE_PATH)
    _LOGGER.info(
        "permission watcher started with %d persisted pending incidents", len(service.pending)
    )
    try:
        run_forever(service)
    except KeyboardInterrupt:
        _LOGGER.info("permission watcher stopping")
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
