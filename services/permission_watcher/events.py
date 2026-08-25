"""Parse and correlate macOS TCC and Application Firewall log events."""

from __future__ import annotations

import json
import re
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import cast

from shared.proc import run_bounded

TCC_PREDICATE = (
    'subsystem == "com.apple.TCC" AND ('
    'eventMessage CONTAINS "AUTHREQ_PROMPTING" OR '
    'eventMessage CONTAINS "AUTHREQ_ATTRIBUTION" OR '
    'eventMessage CONTAINS "AUTHREQ_RESULT")'
)
ALF_PREDICATE = (
    'process == "socketfilterfw" AND ('
    'eventMessage CONTAINS "Prompting" OR '
    'eventMessage CONTAINS "Found matching app, return known verdict")'
)

_BINARY_PATH = re.compile(r'binary_path\s*[:=]\s*(?:"(?P<quoted>[^"]+)"|(?P<bare>[^,}\n]+))')
_SUBJECT = re.compile(r"Sub:\{([^}]+)\}")
_IDENTIFIER = re.compile(r"identifier\s*[:=]\s*([^,}\s]+)")
_REQUEST_ID = re.compile(r"(?:auth_req|request_id|requestID)\s*[:=]\s*([^,}\s\]]+)")
_PROCESS_PID = re.compile(r"(?:processPID|pid)\s*[:=]\s*(\d+)")
_TCC_PHASES = (
    ("AUTHREQ_ATTRIBUTION", "attribution"),
    ("AUTHREQ_PROMPTING", "prompting"),
    ("AUTHREQ_RESULT", "resolved"),
)


class PermissionKind(StrEnum):
    TCC = "TCC"
    ALF = "ALF"

    @property
    def display_name(self) -> str:
        if self is PermissionKind.TCC:
            return "TCC 完全磁盘访问"
        return "ALF 入站"


class EventPhase(StrEnum):
    ATTRIBUTION = "attribution"
    PROMPTING = "prompting"
    RESOLVED = "resolved"


@dataclass(frozen=True)
class PermissionEvent:
    kind: PermissionKind
    phase: EventPhase
    subject: str
    occurred_at: datetime
    correlation_id: str | None = None
    tool: str | None = None


def _parse_timestamp(value: object, *, fallback_to_now: bool = False) -> datetime:
    if not isinstance(value, str):
        if fallback_to_now:
            return datetime.now(UTC)
        raise ValueError("timestamp is not a string")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        if fallback_to_now:
            return datetime.now(UTC)
        raise
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed


def _binary_path(message: str) -> str | None:
    match = _BINARY_PATH.search(message)
    if match is None:
        return None
    return (match.group("quoted") or match.group("bare")).strip()


def _match_value(pattern: re.Pattern[str], message: str) -> str | None:
    match = pattern.search(message)
    return match.group(1) if match is not None else None


def _record_pid(record: dict[str, object]) -> int | None:
    for key in ("processPID", "processID", "pid"):
        value = record.get(key)
        if isinstance(value, int) and not isinstance(value, bool):
            return value
        if isinstance(value, str) and value.isdecimal():
            return int(value)
    return None


def _tcc_phase(message: str) -> EventPhase | None:
    for marker, phase in _TCC_PHASES:
        if re.search(rf"\b{marker}\b", message):
            return EventPhase(phase)
    return None


def _alf_phase(message: str) -> EventPhase | None:
    if "Prompting for a filtering decision" in message:
        return EventPhase.PROMPTING
    if "Found matching app, return known verdict" in message:
        return EventPhase.RESOLVED
    return None


def resolve_pid_path(pid: int) -> str | None:
    """Best-effort executable path for an ALF process pid."""
    try:
        result = run_bounded(
            ["/bin/ps", "-p", str(pid), "-o", "comm="],
            timeout=2,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    path = result.stdout.strip()
    return path if result.returncode == 0 and path else None


class LogEventParser:
    """Correlate multi-line TCC records and ALF verdicts into permission events."""

    def __init__(self, pid_resolver: Callable[[int], str | None] = resolve_pid_path) -> None:
        self._pid_resolver = pid_resolver
        self._tcc_subjects: dict[str, str] = {}
        self._latest_tcc_subject: str | None = None
        self._latest_alf_subject: str | None = None

    def parse(self, kind: PermissionKind, line: str) -> PermissionEvent | None:
        try:
            raw_record = json.loads(line)
        except (json.JSONDecodeError, TypeError):
            return None
        if not isinstance(raw_record, dict):
            return None
        record = cast(dict[str, object], raw_record)
        message = record.get("eventMessage")
        if not isinstance(message, str):
            return None
        when = _parse_timestamp(record.get("timestamp"), fallback_to_now=True)
        if kind is PermissionKind.TCC:
            return self._parse_tcc(message, when)
        return self._parse_alf(message, when, _record_pid(record))

    def _parse_tcc(self, message: str, when: datetime) -> PermissionEvent | None:
        phase = _tcc_phase(message)
        if phase is None:
            return None
        request_id = _match_value(_REQUEST_ID, message)
        binary_path = _binary_path(message)
        subject = (
            _match_value(_SUBJECT, message) or binary_path or _match_value(_IDENTIFIER, message)
        )
        if subject is not None:
            self._remember_tcc_subject(subject, request_id)
        else:
            subject = self._tcc_subjects.get(request_id or "") or self._latest_tcc_subject
        subject = subject or (f"TCC request {request_id}" if request_id else "unknown TCC process")
        tool = binary_path if binary_path is not None and binary_path != subject else None
        return PermissionEvent(PermissionKind.TCC, phase, subject, when, request_id, tool)

    def _remember_tcc_subject(self, subject: str, request_id: str | None) -> None:
        self._latest_tcc_subject = subject
        if request_id is not None:
            self._tcc_subjects[request_id] = subject

    def _parse_alf(
        self, message: str, when: datetime, record_pid: int | None
    ) -> PermissionEvent | None:
        phase = _alf_phase(message)
        if phase is None:
            return None
        pid_text = _match_value(_PROCESS_PID, message)
        pid = int(pid_text) if pid_text is not None else record_pid
        subject = self._alf_subject(pid)
        if phase is EventPhase.PROMPTING:
            self._latest_alf_subject = subject
        correlation_id = str(pid) if pid is not None else None
        return PermissionEvent(PermissionKind.ALF, phase, subject, when, correlation_id)

    def _alf_subject(self, pid: int | None) -> str:
        if pid is None:
            return self._latest_alf_subject or "unknown ALF process"
        return self._pid_resolver(pid) or f"pid {pid}"


def log_stream_commands() -> tuple[list[str], list[str]]:
    return (
        ["/usr/bin/log", "stream", "--style", "ndjson", "--predicate", TCC_PREDICATE],
        ["/usr/bin/log", "stream", "--style", "ndjson", "--predicate", ALF_PREDICATE],
    )
