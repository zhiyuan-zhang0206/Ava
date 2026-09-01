"""Owner-only `.env` write history and out-of-band modification detection.

The JSONL history records `.env` aliases, metadata, and digests only:
configuration values never enter either this file or the unified event stream.
Bootstrap provisioning (`cli.install_cluster` and `cli.enroll`) intentionally
creates a fresh `.env` without audit history or an armed marker, so the guard
remains unarmed until an audited runtime write.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import psutil

from shared.envfile import ENV_LOCK_TIMEOUT_S, env_lock_path
from shared.log import logger
from shared.platform import file_lock
from shared.private_storage import write_private_bytes

_AUDIT_TAIL_BYTES = 64 * 1024


def _env_path() -> Path:
    """Resolve the unit `.env` without adding a runtime-config import cycle."""
    from shared.runtime_config import env_file_path

    return env_file_path()


def _audit_path(env_path: Path) -> Path:
    """Return the per-unit owner-only JSONL audit path."""
    return env_path.with_name(".env.audit.jsonl")


def _armed_path(env_path: Path) -> Path:
    """Return the durable marker that distinguishes armed from fresh homes."""
    return env_path.with_name(".env.audit.armed")


def _env_key_names(env_path: Path) -> list[str]:
    """Return present key names without retaining or emitting their values."""
    if not env_path.exists():
        return []
    names: set[str] = set()
    for line in env_path.read_text(encoding="utf-8", errors="replace").splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            names.add(stripped.split("=", 1)[0].strip())
    return sorted(names)


def _process_metadata() -> tuple[str, str]:
    """Return the current process name and a safe, bounded command line."""
    try:
        process = psutil.Process()
        cmdline = cast("list[object]", process.cmdline())
        executable = Path(str(cmdline[0])).name if cmdline else str(process.name())
        return str(process.name()), f"{executable} [arguments redacted]"[:200]
    except (psutil.Error, OSError):
        return Path(sys.argv[0]).name, ""


def _append_record(audit_path: Path, record: Mapping[str, object]) -> None:
    """Append one durable, owner-only JSONL record."""
    audit_path.parent.mkdir(parents=True, exist_ok=True)
    payload = (json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n").encode()
    descriptor = os.open(audit_path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    try:
        os.fchmod(descriptor, 0o600)
        os.write(descriptor, payload)
    finally:
        os.close(descriptor)


def _write_armed_marker(env_path: Path) -> None:
    """Create or repair the owner-only marker before writing audit history."""
    armed_path = _armed_path(env_path)
    armed_path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(armed_path, os.O_WRONLY | os.O_CREAT, 0o600)
    try:
        os.fchmod(descriptor, 0o600)
    finally:
        os.close(descriptor)


def _emit_audit_event(event_type: str, payload: dict[str, object]) -> None:
    """Send a best-effort JSON-safe audit payload to the unified event stream."""
    from shared.audit_events import insert_event_log

    insert_event_log(event_type=event_type, agent_id=None, source="system", payload=payload)


def _machine_name() -> str:
    """Return a machine label without letting damaged config block detection.

    No Settings/os.environ fallback: a broken Settings is exactly what this
    guard detects, and the unified event stream stamps its own machine field
    regardless — an empty label here only degrades the payload's copy."""
    try:
        from shared.machine import machine_name

        return machine_name()
    except Exception:
        return ""


def _string_names(value: object) -> set[str]:
    """Coerce JSON name arrays while rejecting malformed audit fields."""
    if not isinstance(value, list):
        return set()
    names: set[str] = set()
    for name in cast("list[object]", value):
        if not isinstance(name, str):
            return set()
        names.add(name)
    return names


def _last_official_site(record: dict[str, object]) -> str:
    """Carry an earlier official site through a chain of anomaly records."""
    site = record.get("site", record.get("last_official_site", ""))
    return site if isinstance(site, str) else ""


def record_env_write(
    env_path: Path, keys_written: set[str], keys_removed: set[str], *, site: str
) -> None:
    """Append metadata for an official `.env` write that has already landed.

    `keys_written` and `keys_removed` use the `.env` alias vocabulary. Callers
    invoke this while holding `shared.envfile.env_lock_path`'s lock, so the
    recorded digest describes exactly the bytes that their write completed.
    """
    process, cmdline = _process_metadata()
    digest_after = hashlib.sha256(env_path.read_bytes() if env_path.exists() else b"").hexdigest()
    record: dict[str, object] = {
        "ts": datetime.now(UTC).isoformat(),
        "site": site,
        "pid": os.getpid(),
        "process": process,
        "cmdline": cmdline,
        "keys_written": sorted(keys_written),
        "keys_removed": sorted(keys_removed),
        "keys_after": _env_key_names(env_path),
        "digest_after": digest_after,
    }
    # The marker comes first: a crash can produce an armed repair event, but it
    # cannot leave a history whose deletion recreates the fresh-home branch.
    _write_armed_marker(env_path)
    _append_record(_audit_path(env_path), record)
    _emit_audit_event(
        "env_write",
        {
            "site": site,
            "pid": os.getpid(),
            "process": process,
            "cmdline": cmdline,
            "keys_written": sorted(keys_written),
            "keys_removed": sorted(keys_removed),
            "digest_after": digest_after,
        },
    )


def _last_audit_line(audit_path: Path) -> str | None:
    """Read only the final JSONL line on the config-read hot path."""
    with audit_path.open("rb") as history:
        history.seek(0, os.SEEK_END)
        offset = max(0, history.tell() - _AUDIT_TAIL_BYTES)
        history.seek(offset)
        tail = history.read()
    line = tail.rstrip(b"\r\n").rsplit(b"\n", maxsplit=1)[-1]
    return line.decode("utf-8") if line else None


def last_env_write_record(env_path: Path | None = None) -> dict[str, object] | None:
    """Return the final audit line, or None when the history is absent or empty."""
    audit_path = _audit_path(env_path or _env_path())
    if not audit_path.exists():
        return None
    line = _last_audit_line(audit_path)
    if line is None:
        return None
    record = json.loads(line)
    if not isinstance(record, dict):
        raise TypeError("last .env audit record is not an object")
    return cast("dict[str, object]", record)


def _history_problem(
    env_path: Path, audit_path: Path
) -> tuple[dict[str, object] | None, str | None]:
    """Read the last record and classify damaged armed history for repair."""
    if not audit_path.exists():
        return None, "audit_history_missing"
    try:
        record = last_env_write_record(env_path)
    except (OSError, TypeError, UnicodeDecodeError, json.JSONDecodeError, ValueError):
        return None, "audit_history_corrupt"
    if record is None:
        return None, "audit_history_empty"
    digest = record.get("digest_after", record.get("digest"))
    if not isinstance(digest, str):
        return None, "audit_history_missing_digest"
    return record, None


def _unauthorized_record(
    *,
    current_digest: str,
    current_keys: set[str],
    previous: dict[str, object] | None,
    reason: str | None = None,
) -> dict[str, object]:
    """Build the value-free anomaly record and its next-check digest baseline."""
    previous_keys: set[str] = _string_names(previous.get("keys_after")) if previous else set()
    record: dict[str, object] = {
        "ts": datetime.now(UTC).isoformat(),
        "kind": "unauthorized",
        "digest": current_digest,
        "last_official_site": _last_official_site(previous) if previous else "",
        "keys": sorted(current_keys ^ previous_keys),
        "keys_after": sorted(current_keys),
    }
    if reason is not None:
        record["reason"] = reason
    return record


def _emit_unauthorized_write(detection: dict[str, object]) -> None:
    """Deliver a durable audit anomaly after its JSONL baseline has landed."""
    payload: dict[str, object] = {
        "machine": _machine_name(),
        "last_official_site": detection["last_official_site"],
        "keys": detection["keys"],
    }
    reason = detection.get("reason")
    if isinstance(reason, str):
        payload["reason"] = reason
    _emit_audit_event("env_unauthorized_write", payload)
    if isinstance(reason, str):
        logger.error(".env audit history tamper detected: {}", reason)
    else:
        logger.error(
            "out-of-band .env modification detected after official write at {}",
            detection["last_official_site"],
        )


def check_env_integrity() -> dict[str, object] | None:
    """Return an anomaly record when an armed audit digest no longer matches.

    The guard is deliberately inert until an official write creates the audit
    history. Errors are contained because config reads must not fail merely
    because their anomaly detector cannot inspect a damaged file.
    """
    try:
        env_path = _env_path()
        audit_path = _audit_path(env_path)
        armed_path = _armed_path(env_path)
        with file_lock(env_lock_path(env_path), timeout_s=ENV_LOCK_TIMEOUT_S):
            if not armed_path.exists() and not audit_path.exists():
                return None
            if not armed_path.exists():
                # Upgrade pre-marker audit histories without treating an existing
                # valid official record as an anomaly.
                _write_armed_marker(env_path)
            record, history_reason = _history_problem(env_path, audit_path)
            current = hashlib.sha256(
                env_path.read_bytes() if env_path.exists() else b""
            ).hexdigest()
            current_keys = set(_env_key_names(env_path))
            if history_reason is not None:
                detection = _unauthorized_record(
                    current_digest=current,
                    current_keys=current_keys,
                    previous=None,
                    reason=history_reason,
                )
                # A corrupt or empty file cannot be a trustworthy append target;
                # replace it before persisting the anomaly baseline.
                write_private_bytes(audit_path, b"")
                _append_record(audit_path, detection)
            elif record is None:
                detection = _unauthorized_record(
                    current_digest=current,
                    current_keys=current_keys,
                    previous=None,
                    reason="audit_history_corrupt",
                )
                write_private_bytes(audit_path, b"")
                _append_record(audit_path, detection)
            else:
                expected = record.get("digest_after", record.get("digest"))
                if not isinstance(expected, str):
                    detection = _unauthorized_record(
                        current_digest=current,
                        current_keys=current_keys,
                        previous=None,
                        reason="audit_history_missing_digest",
                    )
                    write_private_bytes(audit_path, b"")
                    _append_record(audit_path, detection)
                elif expected == current:
                    return None
                else:
                    detection = _unauthorized_record(
                        current_digest=current,
                        current_keys=current_keys,
                        previous=record,
                    )
                    _append_record(audit_path, detection)
        _emit_unauthorized_write(detection)
        return detection
    except Exception:
        logger.opt(exception=True).warning("could not inspect .env audit integrity")
        return None
