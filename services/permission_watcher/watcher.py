"""Watch macOS TCC/ALF prompts and deliver their lifecycle through IM notices.

Two unified-log streams feed one queue. Reader threads own only their stream
parser; the main thread exclusively owns persistent pending state and database
delivery, so no permission incident is mutated concurrently.
"""

from __future__ import annotations

import json
import logging
import queue
import signal
import subprocess
import sys
import threading
from collections.abc import Callable
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
from services.permission_watcher.notices import insert_notice, read_db_url

_LOGGER = logging.getLogger("ava.permission_watcher")

DEDUPE_WINDOW = timedelta(minutes=5)
ESCALATION_AFTER = timedelta(minutes=30)
STATE_PATH = Path.home() / ".ava" / "state" / "permission-watcher.json"


@dataclass
class PendingPermission:
    kind: PermissionKind
    subject: str
    first_seen: datetime
    last_seen: datetime
    correlation_id: str | None
    notified: bool = False
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
            first_seen=_parse_timestamp(payload["first_seen"]),
            last_seen=_parse_timestamp(payload["last_seen"]),
            correlation_id=(
                str(payload["correlation_id"])
                if payload.get("correlation_id") is not None
                else None
            ),
            notified=bool(payload["notified"]),
            escalated=bool(payload["escalated"]),
        )


NoticeSender = Callable[[str, str], None]


class PermissionWatcher:
    """Own persistent pending incidents and emit each lifecycle transition once."""

    def __init__(self, state_path: Path, send_notice: NoticeSender) -> None:
        self._state_path = state_path
        self._send_notice = send_notice
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
                since_last_event = event.occurred_at - existing.last_seen
                existing.last_seen = event.occurred_at
                existing.correlation_id = event.correlation_id or existing.correlation_id
                self._save()
                if not existing.notified:
                    self._notify_pending(existing)
                    return
                if since_last_event <= DEDUPE_WINDOW:
                    return
                self._send_notice("macOS 权限弹窗待处理", self._pending_content(existing))
                return
            pending = PendingPermission(
                event.kind,
                event.subject,
                event.occurred_at,
                event.occurred_at,
                event.correlation_id,
            )
            self.pending[key] = pending
            self._save()
            self._notify_pending(pending)
            return
        pending = self._find_pending(event)
        if pending is None:
            return
        if pending.notified:
            self._send_notice(
                "macOS 权限弹窗已处理",
                f"权限弹窗已处理: {pending.subject}\n弹窗类型: {pending.kind.display_name}",
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

    @staticmethod
    def _pending_content(pending: PendingPermission) -> str:
        appeared = pending.last_seen.astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")
        return (
            f"弹窗类型: {pending.kind.display_name}\n"
            f"进程: {pending.subject}\n"
            f"出现时间: {appeared}\n"
            "建议动作: 请在 Mac 上点击允许/拒绝。"
        )

    def _notify_pending(self, pending: PendingPermission) -> None:
        self._send_notice("macOS 权限弹窗待处理", self._pending_content(pending))
        pending.notified = True
        self._save()

    def check_timeouts(self, now: datetime) -> None:
        changed = False
        for pending in self.pending.values():
            if not pending.notified:
                self._notify_pending(pending)
                continue
            if pending.escalated or now - pending.first_seen < ESCALATION_AFTER:
                continue
            self._send_notice(
                "macOS 权限弹窗仍未处理",
                f"权限弹窗仍未处理 (已挂起 30 分钟): {pending.subject}\n"
                f"弹窗类型: {pending.kind.display_name}\n请在 Mac 上操作。",
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
    db_url = read_db_url()

    def send_notice(title: str, content: str) -> None:
        insert_notice(db_url, title, content)

    service = PermissionWatcher(STATE_PATH, send_notice)
    service.check_timeouts(datetime.now(UTC))
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
