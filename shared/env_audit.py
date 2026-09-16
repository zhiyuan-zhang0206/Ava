"""Owner-only `.env` write history and out-of-band modification detection.

The JSONL history records `.env` aliases, metadata, digests, the initiating
credential fact, and — for non-sensitive fields only — the old→new value diff:
configuration values of sensitive (or unregistered) keys never enter this file,
and configuration values never enter the unified event stream at all.
Bootstrap provisioning (`cli.install_cluster` and `cli.enroll`) intentionally
creates a fresh `.env` without audit history or an armed marker, so the guard
remains unarmed until an audited runtime write.
"""

from __future__ import annotations

import functools
import hashlib
import json
import os
import sys
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import psutil

from shared.envfile import ENV_LOCK_TIMEOUT_S, env_line_key, env_lock_path
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
        key = env_line_key(line)
        if key is not None:
            names.add(key)
    return sorted(names)


def env_values_from_text(text: str) -> dict[str, str]:
    """Parse `key=value` text into `{alias: value}` for the audit value diff.

    Surrounding matching quotes are stripped, matching how the write paths
    render values. This feeds ONLY the local audit record — `record_env_write`
    drops the values of sensitive and unregistered aliases before persisting.
    """
    values: dict[str, str] = {}
    for line in text.splitlines():
        key = env_line_key(line)
        if key is None:
            continue
        _, _, raw = line.partition("=")
        raw = raw.strip()
        if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in {"'", '"'}:
            raw = raw[1:-1]
        values[key] = raw
    return values


@functools.cache
def _load_alias_metadata() -> dict[str, tuple[str, bool]]:
    from shared.config.metadata import get_config_metadata

    return {meta.env_var: (meta.scope, meta.sensitive) for meta in get_config_metadata()}


def _alias_metadata() -> dict[str, tuple[str, bool]]:
    """`alias -> (scope, sensitive)` from the config registry, possibly empty.

    A value is recorded only for a registered field explicitly marked
    `sensitive: false`. Every failure path yields an empty mapping, which
    withholds every value (fail closed); a successful build is cached because
    the registry derives from class declarations (a failed one is not, so a
    later call retries rather than withholding for the process lifetime).
    """
    try:
        return _load_alias_metadata()
    except Exception:
        logger.opt(exception=True).warning(
            "could not load config metadata for the .env audit — recording names only"
        )
        return {}


def _audit_changes(
    changes: Sequence[Mapping[str, object]] | None,
) -> list[dict[str, object]] | None:
    """Redact a raw `{alias, old, new}` diff into its auditable form.

    Values survive only for aliases registered with `sensitive: false`; every
    other alias — sensitive, or not registered at all — is recorded by name
    with `old`/`new` withheld, so the record can still answer "this key
    changed" without becoming a second place secrets live.
    """
    if changes is None:
        return None
    metadata = _alias_metadata()
    redacted: list[dict[str, object]] = []
    for change in changes:
        alias = str(change["alias"])
        meta = metadata.get(alias)
        recordable = meta is not None and meta[1] is False
        redacted.append(
            {
                "alias": alias,
                "scope": meta[0] if meta is not None else None,
                "sensitive": meta[1] if meta is not None else None,
                "old": change.get("old") if recordable else None,
                "new": change.get("new") if recordable else None,
            }
        )
    return redacted


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
    env_path: Path,
    keys_written: set[str],
    keys_removed: set[str],
    *,
    site: str,
    actor: str | None = None,
    trace_id: str | None = None,
    changes: Sequence[Mapping[str, object]] | None = None,
) -> None:
    """Append metadata for an official `.env` write that has already landed.

    `keys_written` and `keys_removed` use the `.env` alias vocabulary. Callers
    invoke this while holding `shared.envfile.env_lock_path`'s lock, so the
    recorded digest describes exactly the bytes that their write completed.
    `changes` is the raw `{alias, old, new}` diff the caller captured before its
    rewrite; values are redacted here (only `sensitive: false` fields keep
    theirs). `actor` names the initiating credential fact
    (`user_session:administrator`, `cluster_bearer:administrator`, `cli:zzy`)
    and `trace_id` the gateway request, when the write had one.
    """
    process, cmdline = _process_metadata()
    digest_after = hashlib.sha256(env_path.read_bytes() if env_path.exists() else b"").hexdigest()
    record: dict[str, object] = {
        "ts": datetime.now(UTC).isoformat(),
        "site": site,
        "pid": os.getpid(),
        "process": process,
        "cmdline": cmdline,
        "actor": actor,
        "trace_id": trace_id,
        "keys_written": sorted(keys_written),
        "keys_removed": sorted(keys_removed),
        "keys_after": _env_key_names(env_path),
        "digest_after": digest_after,
    }
    audited_changes = _audit_changes(changes)
    if audited_changes is not None:
        record["changed"] = audited_changes
    # The marker comes first: a crash can produce an armed repair event, but it
    # cannot leave a history whose deletion recreates the fresh-home branch.
    _write_armed_marker(env_path)
    _append_record(_audit_path(env_path), record)
    _emit_audit_event(
        "env_write",
        {
            "site": site,
            "actor": actor,
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


def read_env_write_records(limit: int, env_path: Path | None = None) -> list[dict[str, object]]:
    """The newest `limit` audit records for `env_path`, newest first.

    Reads a bounded tail window (a record is far smaller than `_AUDIT_TAIL_BYTES`),
    so cost is independent of history size. A truncated first line (the window cut
    a record) is skipped by the same per-line tolerance that drops any corrupt
    line; a missing history returns [] (nothing has been written yet).
    """
    if limit < 1:
        raise ValueError("limit must be >= 1")
    audit_path = _audit_path(env_path or _env_path())
    if not audit_path.exists():
        return []
    records: list[dict[str, object]] = []
    text = audit_path.read_bytes()[-_AUDIT_TAIL_BYTES:].decode("utf-8", errors="replace")
    for line in reversed(text.splitlines()):
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(record, dict):
            continue
        records.append(cast("dict[str, object]", record))
        if len(records) >= limit:
            break
    return records


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
