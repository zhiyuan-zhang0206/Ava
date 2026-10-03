"""execute_code lifecycle, exec child and editable-install guard events."""

from __future__ import annotations

from typing import Literal, TypedDict

from base.events.vocabulary import EventSpec, telemetry_event


class ExecPayload(TypedDict):
    """`exec` / `code` payload — agent/graph/exec/node.py."""

    body: str
    ok: bool
    duration_seconds: float


class ExecFailed(TypedDict):
    """`exec_failed` payload."""

    exc_type: str
    body: str


class ExecEnvelope(TypedDict):
    """`exec_envelope` payload — request/result transfer cost."""

    envelope: Literal["request", "result"]
    op: Literal["read", "write"]
    size_bytes: int
    serialize_ms: float


class ExecChildBoot(TypedDict):
    """`exec_child_boot` payload — child bootstrap duration before agent code."""

    duration_ms: float


class ExecRequestQuarantine(TypedDict):
    """`exec_request_quarantine` payload — stale exec request evidence preserved."""

    reason: str
    event_dir: str
    sources: list[str]
    vanished: list[str]


class ExecSubprocessKilled(TypedDict):
    """`exec_subprocess_killed` payload — a child survived the signal grace
    and the parent SIGKILLed its process group."""

    pid: int
    grace: float


class ExecMemoryGuardKilled(TypedDict):
    """`exec_memory_guard_killed` payload — the host memory guard killed the largest
    exec process domain at critical system memory pressure."""

    agent_id: int | None
    pid: int
    footprint_bytes: int
    running: int
    pressure: str


class SyntaxFix(TypedDict):
    """`syntax_fix` payload."""

    fixes: str


EVENTS: dict[str, EventSpec] = {
    # exec lifecycle
    "exec": telemetry_event("exec", "execute_code succeeded", payload=ExecPayload, persist=True),
    "exec_failed": telemetry_event(
        "exec_failed",
        "execute_code failed",
        payload=ExecFailed,
        tier="anomaly",
        persist=True,
    ),
    "exec_envelope": telemetry_event(
        "exec_envelope",
        "exec envelope transfer cost (size + serialize time) — request snapshot / result delta",
        payload=ExecEnvelope,
    ),
    "exec_child_boot": telemetry_event(
        "exec_child_boot",
        "exec child bootstrap duration before agent-authored code",
        payload=ExecChildBoot,
        tier="noise",
    ),
    "exec_request_quarantine": telemetry_event(
        "exec_request_quarantine",
        "stale exec request evidence preserved under the explicit quarantine",
        payload=ExecRequestQuarantine,
    ),
    "exec_request_bounded_quarantine": telemetry_event(
        "exec_request_bounded_quarantine",
        "an unreadable exec request envelope past the bounded-disposition bound "
        "(twice the exec node timeout, no live process reference, no live host "
        "process) was quarantined without review — the bytes are preserved with "
        "a receipt and the recovery path no longer defers on it; off via "
        "AVA_EXEC_REQUEST_BOUNDED_QUARANTINE_ENABLED restores unbounded retention",
        tier="anomaly",
    ),
    "exec_cancelled": telemetry_event(
        "exec_cancelled", "execute_code cancelled", tier="anomaly", persist=True
    ),
    "exec(timeout)": telemetry_event(
        "exec(timeout)",
        "historical parenthesized name (migration target)",
        tier="anomaly",
        site=(
            "Legacy bracketed name: the pre-W8-rename value, still a "
            "migrate_events.py mapping target and present in existing DB rows. New "
            "code must not emit it; the registration survives only to backfill the "
            "metric."
        ),
        persist=True,
    ),
    "exec(failed)": telemetry_event(
        "exec(failed)",
        "historical parenthesized name (migration target)",
        tier="anomaly",
        site=(
            "Legacy bracketed name: the pre-W8-rename value, still a "
            "migrate_events.py mapping target and present in existing DB rows. New "
            "code must not emit it; the registration survives only to backfill the "
            "metric."
        ),
        persist=True,
    ),
    "exec(cancelled)": telemetry_event(
        "exec(cancelled)",
        "historical parenthesized name (migration target)",
        tier="anomaly",
        site=(
            "Legacy bracketed name: the pre-W8-rename value, still a "
            "migrate_events.py mapping target and present in existing DB rows. New "
            "code must not emit it; the registration survives only to backfill the "
            "metric."
        ),
        persist=True,
    ),
    "exec(thread-stuck)": telemetry_event(
        "exec(thread-stuck)",
        "historical parenthesized name (migration target)",
        tier="anomaly",
        site=(
            "Legacy bracketed name: the pre-W8-rename value, still a "
            "migrate_events.py mapping target and present in existing DB rows. New "
            "code must not emit it; the registration survives only to backfill the "
            "metric."
        ),
    ),
    "exec_timeout": telemetry_event(
        "exec_timeout", "execute_code timed out", tier="anomaly", persist=True
    ),
    "exec_node_timeout": telemetry_event(
        "exec_node_timeout", "node-level timeout", tier="anomaly", persist=True
    ),
    "exec_subprocess_killed": telemetry_event(
        "exec_subprocess_killed",
        "exec child survived the signal grace period and was SIGKILLed",
        payload=ExecSubprocessKilled,
        tier="anomaly",
        persist=True,
    ),
    "exec_memory_guard_killed": telemetry_event(
        "exec_memory_guard_killed",
        "the host memory guard killed the largest exec process domain at critical "
        "system memory pressure",
        payload=ExecMemoryGuardKilled,
        tier="anomaly",
    ),
    "code": telemetry_event(
        "code", "LLM generated code block", payload=ExecPayload, tier="noise", persist=True
    ),
    # label-fallback events kept in the registry
    "text": telemetry_event("text", "LLM text output", tier="noise"),
    "syntax_fix": telemetry_event(
        "syntax_fix",
        "syntax repair executed",
        payload=SyntaxFix,
        tier="noise",
        persist=True,
    ),
    "editable_pth_repaired": telemetry_event(
        "editable_pth_repaired",
        "poisoned editable-install pointer repaired to the prod source root",
        tier="anomaly",
        site="base/deploy/release/editable_install.py:repair_editable_ava_pth",
    ),
    "editable_direct_url_repaired": telemetry_event(
        "editable_direct_url_repaired",
        "poisoned editable-install direct_url repaired to the prod source root",
        tier="anomaly",
        site="base/deploy/release/editable_install.py:repair_editable_direct_url",
    ),
    "exec_editable_install_poisoned": telemetry_event(
        "exec_editable_install_poisoned",
        "poisoned editable install repaired before an exec child spawn",
        tier="anomaly",
        site="base/deploy/release/editable_install.py:guard_editable_install",
    ),
}
