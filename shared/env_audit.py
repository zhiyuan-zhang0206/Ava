"""Owner-only `.env` write history and out-of-band modification detection.

The JSONL history records `.env` aliases, metadata, and digests only:
configuration values never enter either this file or the unified event stream.
Bootstrap provisioning (`cli.install_cluster` and `cli.enroll`) intentionally
creates a fresh `.env` without a history, so the guard remains unarmed until an
audited runtime write.
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


def _env_path() -> Path:
    """Resolve the unit `.env` without adding a runtime-config import cycle."""
    from shared.runtime_config import env_file_path

    return env_file_path()


def _audit_path(env_path: Path) -> Path:
    """Return the per-unit owner-only JSONL audit path."""
    return env_path.with_name(".env.audit.jsonl")


def _env_key_names(env_path: Path) -> list[str]:
    """Return present key names without retaining or emitting their values."""
    if not env_path.exists():
        return []
    names: set[str] = set()
    for line in env_path.read_text(encoding="utf-8").splitlines():
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


def _emit_audit_event(event_type: str, payload: dict[str, object]) -> None:
    """Send a best-effort JSON-safe audit payload to the unified event stream."""
    from shared.audit_events import insert_event_log

    insert_event_log(event_type=event_type, agent_id=None, source="system", payload=payload)


def _machine_name() -> str:
    """Return a machine label without letting damaged config block detection."""
    try:
        from shared.machine import machine_name

        return machine_name()
    except Exception:
        return os.environ.get("AVA_MACHINE_NAME", "")


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


def record_env_write(keys_written: set[str], keys_removed: set[str], *, site: str) -> None:
    """Append metadata for an official `.env` write that has already landed.

    `keys_written` and `keys_removed` use the `.env` alias vocabulary. Callers
    invoke this while holding `shared.envfile.env_lock_path`'s lock, so the
    recorded digest describes exactly the bytes that their write completed.
    """
    env_path = _env_path()
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


def last_env_write_record() -> dict[str, object] | None:
    """Return the final audit line, or None before auditing has been armed."""
    audit_path = _audit_path(_env_path())
    if not audit_path.exists():
        return None
    lines = audit_path.read_text(encoding="utf-8").splitlines()
    if not lines:
        return None
    return json.loads(lines[-1])


def check_env_integrity() -> dict[str, object] | None:
    """Return an anomaly record when an armed audit digest no longer matches.

    The guard is deliberately inert until an official write creates the audit
    history. Errors are contained because config reads must not fail merely
    because their anomaly detector cannot inspect a damaged file.
    """
    try:
        env_path = _env_path()
        audit_path = _audit_path(env_path)
        if not audit_path.exists():
            return None
        with file_lock(env_lock_path(env_path), timeout_s=ENV_LOCK_TIMEOUT_S):
            record = last_env_write_record()
            if record is None:
                return None
            current = hashlib.sha256(
                env_path.read_bytes() if env_path.exists() else b""
            ).hexdigest()
            expected = record.get("digest_after", record.get("digest"))
            if expected == current:
                return None
            if not isinstance(expected, str):
                logger.warning("last .env audit record has no digest")
                return None
            current_keys = set(_env_key_names(env_path))
            prior_keys = _string_names(record.get("keys_after"))
            detection: dict[str, object] = {
                "ts": datetime.now(UTC).isoformat(),
                "kind": "unauthorized",
                "digest": current,
                "last_official_site": _last_official_site(record),
                "keys": sorted(current_keys ^ prior_keys),
                "keys_after": sorted(current_keys),
            }
            _append_record(audit_path, detection)
        _emit_audit_event(
            "env_unauthorized_write",
            {
                "machine": _machine_name(),
                "last_official_site": detection["last_official_site"],
                "keys": detection["keys"],
            },
        )
        logger.error(
            "out-of-band .env modification detected after official write at {}",
            detection["last_official_site"],
        )
        return detection
    except Exception:
        logger.opt(exception=True).warning("could not inspect .env audit integrity")
        return None
