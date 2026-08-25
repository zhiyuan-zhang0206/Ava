"""Watch macOS TCC/ALF prompts and deliver their lifecycle through alerts.

Two unified-log streams feed one queue. Reader threads own only their stream
parser; the main thread exclusively owns persistent incident state and alert
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
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal, cast

import httpx
from dotenv import dotenv_values

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

RECUR_SILENCE = timedelta(hours=12)
PENDING_STALE_AFTER = timedelta(hours=24)
_RESOLVED_RETENTION = timedelta(hours=48)
_DEFAULT_GATEWAY_PORT = 8000
_ALERTS_TOKEN_ENV = "AVA_OPS_ALERTS_WEBHOOK_TOKEN"  # noqa: S105 — env key, not a token value
_RETRY_BACKOFF_SECONDS = 2.0
ENV_PATH = Path.home() / ".ava" / ".env"
STATE_PATH = Path.home() / ".ava" / "state" / "permission-watcher.json"

AlertPoster = Callable[[dict[str, object]], None]
PendingMode = Literal["full", "silent"]


def _alerts_url() -> str:
    try:
        from shared.config import settings

        gateway_port = settings.gateway.gateway_port
    except Exception:
        # launchd may start the host-global watcher without enough cluster
        # environment for the full settings singleton to construct.
        gateway_port = _DEFAULT_GATEWAY_PORT
    return f"http://127.0.0.1:{gateway_port}/api/alerts"


def _alerts_token(env_path: Path) -> str:
    value = dotenv_values(env_path).get(_ALERTS_TOKEN_ENV)
    if value:
        return value

    # The standalone launchd service reads the explicit prod env file first;
    # Settings preserves the process-environment fallback without creating a
    # second raw-environment access pattern in services code.
    from shared.config import settings

    token = settings.alerts.webhook_token
    return token.get_secret_value() if token is not None else ""


def post_alert(payload: dict[str, object], *, env_path: Path = ENV_PATH) -> None:
    """POST one alert payload to the loopback gateway, retrying once."""
    headers = {"X-Alerts-Token": _alerts_token(env_path)}
    for attempt in range(2):
        try:
            response = httpx.post(
                _alerts_url(),
                json=payload,
                headers=headers,
                timeout=10.0,
            )
            response.raise_for_status()
            return
        except Exception as exc:
            if attempt == 0:
                time.sleep(_RETRY_BACKOFF_SECONDS)
                continue
            _LOGGER.warning(
                "permission alert delivery failed after retry: %s",
                type(exc).__name__,
            )
            raise


@dataclass
class PendingPermission:
    kind: PermissionKind
    subject: str
    tool: str | None
    first_seen: datetime
    last_seen: datetime
    correlation_id: str | None
    mode: PendingMode = "full"
    alert_posted: bool = False

    def to_json(self) -> dict[str, object]:
        payload = asdict(self)
        payload["kind"] = self.kind.value
        payload["first_seen"] = self.first_seen.isoformat()
        payload["last_seen"] = self.last_seen.isoformat()
        return payload

    @classmethod
    def from_json(cls, payload: dict[str, object]) -> PendingPermission:
        mode = str(payload.get("mode", "full"))
        if mode not in ("full", "silent"):
            raise ValueError(f"unknown permission watcher pending mode: {mode}")
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
            mode=mode,
            alert_posted=bool(payload.get("alert_posted", False)),
        )


class PermissionWatcher:
    """Own pending incidents and emit each alert lifecycle transition once."""

    def __init__(self, state_path: Path, post_alert: AlertPoster) -> None:
        self._state_path = state_path
        self._post_alert = post_alert
        self.pending, self.resolved = self._load()

    @staticmethod
    def _key(kind: PermissionKind, subject: str) -> str:
        return f"{kind.value}:{subject}"

    def _load(self) -> tuple[dict[str, PendingPermission], dict[str, datetime]]:
        if not self._state_path.exists():
            return {}, {}
        raw_payload = json.loads(self._state_path.read_text())
        if not isinstance(raw_payload, dict):
            raise TypeError("permission watcher state is not an object")
        payload = cast(dict[str, object], raw_payload)
        pending = payload["pending"]
        if not isinstance(pending, dict):
            raise TypeError("permission watcher state pending field is not an object")
        result: dict[str, PendingPermission] = {}
        pending_cutoff = datetime.now(UTC) - PENDING_STALE_AFTER
        for key, value in cast(dict[object, object], pending).items():
            if not isinstance(key, str) or not isinstance(value, dict):
                raise TypeError("permission watcher pending entry is malformed")
            incident = PendingPermission.from_json(cast(dict[str, object], value))
            if incident.first_seen < pending_cutoff:
                _LOGGER.info(
                    "dropping stale pending incident: %s (first_seen %s)",
                    key,
                    incident.first_seen.isoformat(),
                )
                continue
            result[key] = incident
        resolved = payload.get("resolved", {})
        if not isinstance(resolved, dict):
            raise TypeError("permission watcher state resolved field is not an object")
        resolved_result: dict[str, datetime] = {}
        for key, value in cast(dict[object, object], resolved).items():
            if not isinstance(key, str) or not isinstance(value, str):
                raise TypeError("permission watcher resolved entry is malformed")
            resolved_result[key] = _parse_timestamp(value)
        return result, resolved_result

    def _save(self, *, now: datetime) -> None:
        cutoff = now - _RESOLVED_RETENTION
        self.resolved = {
            key: resolved_at for key, resolved_at in self.resolved.items() if resolved_at >= cutoff
        }
        self._state_path.parent.mkdir(parents=True, exist_ok=True)
        temp = self._state_path.with_suffix(".tmp")
        temp.write_text(
            json.dumps(
                {
                    "version": 1,
                    "pending": {key: value.to_json() for key, value in self.pending.items()},
                    "resolved": {key: value.isoformat() for key, value in self.resolved.items()},
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
                self._save(now=event.occurred_at)
                _LOGGER.debug(
                    "permission prompt repeated: kind=%s subject=%s tool=%s",
                    existing.kind.value,
                    existing.subject,
                    existing.tool,
                )
                if existing.mode == "full" and not existing.alert_posted:
                    self._post_firing(existing)
                return
            last_resolved_at = self.resolved.get(key)
            mode: PendingMode = "full"
            if (
                last_resolved_at is not None
                and event.occurred_at - last_resolved_at < RECUR_SILENCE
            ):
                mode = "silent"
            pending = PendingPermission(
                kind=event.kind,
                subject=event.subject,
                tool=event.tool,
                first_seen=event.occurred_at,
                last_seen=event.occurred_at,
                correlation_id=event.correlation_id,
                mode=mode,
            )
            self.pending[key] = pending
            self._save(now=event.occurred_at)
            if mode == "silent":
                if last_resolved_at is None:
                    raise RuntimeError("silent permission incident has no resolution timestamp")
                _LOGGER.info(
                    "suppressing repeat alert for %s (resolved %s)",
                    key,
                    last_resolved_at.isoformat(),
                )
                return
            _LOGGER.info(
                "permission prompt: kind=%s subject=%s tool=%s",
                pending.kind.value,
                pending.subject,
                pending.tool,
            )
            self._post_firing(pending)
            return
        pending = self._find_pending(event)
        if pending is None:
            _LOGGER.debug(
                "permission prompt resolution without pending incident: kind=%s subject=%s",
                event.kind.value,
                event.subject,
            )
            return
        if pending.mode == "full" and pending.alert_posted:
            try:
                self._post_alert(self._alert_payload("resolved", pending, event.occurred_at))
            except Exception as exc:
                _LOGGER.warning(
                    "permission resolved alert post failed: kind=%s subject=%s error=%s",
                    pending.kind.value,
                    pending.subject,
                    type(exc).__name__,
                )
        pending_key = self._key(pending.kind, pending.subject)
        self.resolved[pending_key] = event.occurred_at
        self.pending.pop(pending_key, None)
        self._save(now=event.occurred_at)
        _LOGGER.info(
            "permission prompt resolved: kind=%s subject=%s",
            pending.kind.value,
            pending.subject,
        )

    def _post_firing(self, pending: PendingPermission) -> None:
        try:
            self._post_alert(self._alert_payload("firing", pending))
        except Exception as exc:
            _LOGGER.warning(
                "permission firing alert post failed: kind=%s subject=%s error=%s",
                pending.kind.value,
                pending.subject,
                type(exc).__name__,
            )
            return
        pending.alert_posted = True
        self._save(now=pending.last_seen)

    @staticmethod
    def _alert_payload(
        status: Literal["firing", "resolved"],
        pending: PendingPermission,
        ends_at: datetime | None = None,
    ) -> dict[str, object]:
        appeared = pending.first_seen.astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")
        tool = pending.tool or "未知"
        summary = (
            f"弹窗类型: {pending.kind.display_name}\n"
            f"进程: {pending.subject}\n"
            f"触发工具: {tool}\n"
            f"出现时间: {appeared}\n"
            "建议动作: 请在 Mac 上点击允许/拒绝。"
        )
        return {
            "source": "permission-watcher",
            "status": status,
            "alerts": [
                {
                    "status": status,
                    "labels": {
                        "alertname": "permission-prompt",
                        "severity": "warning",
                        "kind": pending.kind.value,
                        "subject": pending.subject,
                    },
                    "annotations": {"summary": summary},
                    "startsAt": pending.first_seen.isoformat(),
                    "endsAt": ends_at.isoformat() if ends_at is not None else "",
                }
            ],
        }

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
            item = items.get()
            if isinstance(item, _StreamFailure):
                raise ChildProcessError(
                    f"{item.kind.value} log stream exited with status {item.returncode}"
                )
            service.observe(item)
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
    service = PermissionWatcher(STATE_PATH, post_alert)
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
