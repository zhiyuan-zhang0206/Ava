"""Validated, atomic persistence for canonical coding-session owner records."""

from __future__ import annotations

import contextlib
import datetime as dt
import hashlib
import json
import os
import re
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Never, cast

SCHEMA_VERSION = 1
_TOOL_RE = re.compile(r"[a-z][a-z0-9-]*")
PersistedStatus = Literal["launching", "active", "terminal"]


class InvalidCodingSessionOwnerError(RuntimeError):
    """The canonical owner record is malformed and therefore fails closed."""


@dataclass(frozen=True)
class CodingSessionKey:
    """Canonical identity for one coding tool in one cluster workspace."""

    cluster: str
    workspace: str
    tool: str


@dataclass(frozen=True)
class CodingSessionOwner:
    """One immutable snapshot of a canonical owner generation."""

    key: CodingSessionKey
    status: Literal["inactive", "launching", "active", "terminal", "invalid"]
    generation: str | None = None
    owner_agent_id: int | None = None
    display_label: str | None = None
    expected_suffix: str | None = None
    session_id: int | None = None
    session_name: str | None = None
    supervisor_session_id: int | None = None
    supervisor_session_name: str | None = None
    state_dir: Path | None = None
    tasks_file: Path | None = None
    work_file: Path | None = None
    created_at: dt.datetime | None = None
    expires_at: dt.datetime | None = None
    terminalized_at: dt.datetime | None = None
    terminal_reason: str | None = None
    error: str | None = None


def _invalid(message: str) -> Never:
    raise ValueError(message)


def canonical_key(
    workspace: str | Path,
    *,
    tool: str,
    cluster: str | Path | None = None,
) -> CodingSessionKey:
    """Build the canonical record key; basename never participates."""
    if not _TOOL_RE.fullmatch(tool):
        raise ValueError("tool must be a lowercase slug")
    if cluster is None:
        from shared.paths import ava_home

        cluster_path = ava_home()
    else:
        cluster_path = Path(cluster).expanduser()
    return CodingSessionKey(
        cluster=str(cluster_path.resolve()),
        workspace=str(Path(workspace).expanduser().resolve()),
        tool=tool,
    )


def _key_digest(key: CodingSessionKey) -> str:
    identity = json.dumps(
        {"cluster": key.cluster, "tool": key.tool, "workspace": key.workspace},
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(identity.encode()).hexdigest()


def display_label(workspace: str) -> str:
    label = re.sub(r"[^a-z0-9]+", "-", Path(workspace).name.lower()).strip("-")
    return (label or "workspace")[:40].rstrip("-")


def expected_suffix(key: CodingSessionKey, generation: str) -> str:
    return f"{key.tool}-{display_label(key.workspace)}-{generation.replace('-', '')[:8]}"


def supervisor_suffix(key: CodingSessionKey, generation: str) -> str:
    identity = f"{key.cluster}\0{key.workspace}\0{generation}"
    return f"{key.tool}-owner-{hashlib.sha256(identity.encode()).hexdigest()[:12]}"


def full_session_name(owner_agent_id: int, session_id: int, suffix: str) -> str:
    """Build the full PTY handle behind an owner-scoped numeric session id."""
    return f"ava-agent-{owner_agent_id}-shell-{session_id}-{suffix}"


def _host_owner_dir() -> Path:
    from shared.config import settings

    root = Path(settings.general.cluster_registry).expanduser().parent / "coding-session-owners"
    root.mkdir(parents=True, exist_ok=True)
    with contextlib.suppress(OSError):
        root.chmod(0o700)
    return root


def state_path(key: CodingSessionKey) -> Path:
    return _host_owner_dir() / f"{_key_digest(key)}.json"


def lock_path(key: CodingSessionKey) -> Path:
    return _host_owner_dir() / f"{_key_digest(key)}.lock"


def generation_state_dir(key: CodingSessionKey, generation: str) -> Path:
    """Private mutable tool state for exactly one owner generation."""
    return Path(key.cluster) / "run" / "coding-tools" / key.tool / _key_digest(key) / generation


def _timestamp(value: object, field: str) -> dt.datetime:
    if not isinstance(value, str):
        _invalid(f"{field} must be an RFC3339 string")
    parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        _invalid(f"{field} must carry a timezone")
    return parsed.astimezone(dt.UTC)


def _optional_timestamp(value: object, field: str) -> dt.datetime | None:
    return None if value is None else _timestamp(value, field)


def _required_str(raw: dict[str, object], field: str) -> str:
    value = raw.get(field)
    if not isinstance(value, str) or not value:
        _invalid(f"{field} must be a non-empty string")
    return value


def _optional_positive_int(raw: dict[str, object], field: str) -> int | None:
    value = raw.get(field)
    if value is None:
        return None
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        _invalid(f"{field} must be a non-negative integer or null")
    return value


def _parse_generation_identity(
    key: CodingSessionKey,
    raw: dict[str, object],
) -> tuple[str, int, str, str]:
    generation = _required_str(raw, "generation")
    try:
        canonical_generation = str(uuid.UUID(generation))
    except ValueError:
        _invalid("generation must be a UUID")
    if generation != canonical_generation:
        _invalid("generation must use canonical UUID spelling")
    owner_agent_id = _optional_positive_int(raw, "owner_agent_id")
    if owner_agent_id is None:
        _invalid("owner_agent_id is required")
    label = _required_str(raw, "display_label")
    if label != display_label(key.workspace):
        _invalid("display_label does not match the canonical workspace")
    suffix = _required_str(raw, "expected_suffix")
    if suffix != expected_suffix(key, generation):
        _invalid("expected_suffix does not match the canonical generation")
    return generation, owner_agent_id, label, suffix


def _parse_session_identity(
    raw: dict[str, object],
    status: PersistedStatus,
    owner_agent_id: int,
    suffix: str,
) -> tuple[int | None, str | None]:
    session_id = _optional_positive_int(raw, "session_id")
    name_raw = raw.get("session_name")
    if name_raw is not None and not isinstance(name_raw, str):
        _invalid("session_name must be a string or null")
    name = name_raw
    if status == "active" and (session_id is None or not name):
        _invalid("active owner must carry a session id and full name")
    if (session_id is None) != (name is None):
        _invalid("session id and full name must be published together")
    if session_id is not None and name != full_session_name(owner_agent_id, session_id, suffix):
        _invalid("session_name does not match its owner, id, and generation")
    if status == "launching" and session_id is not None:
        _invalid("launching owner cannot publish a session handle")
    return session_id, name


def _parse_supervisor_identity(
    key: CodingSessionKey,
    generation: str,
    raw: dict[str, object],
    status: PersistedStatus,
    owner_agent_id: int,
) -> tuple[int | None, str | None]:
    session_id = _optional_positive_int(raw, "supervisor_session_id")
    name_raw = raw.get("supervisor_session_name")
    if name_raw is not None and not isinstance(name_raw, str):
        _invalid("supervisor_session_name must be a string or null")
    name = name_raw
    if (session_id is None) != (name is None):
        _invalid("supervisor session id and full name must be published together")
    if status == "active" and session_id is None:
        _invalid("active owner must carry its supervisor handle")
    if session_id is not None and name != full_session_name(
        owner_agent_id,
        session_id,
        supervisor_suffix(key, generation),
    ):
        _invalid("supervisor full name does not match its owner, id, and generation")
    return session_id, name


def _parse_terminal_metadata(
    raw: dict[str, object],
    status: PersistedStatus,
) -> tuple[dt.datetime | None, str | None]:
    reason_raw = raw.get("terminal_reason")
    if reason_raw is not None and not isinstance(reason_raw, str):
        _invalid("terminal_reason must be a string or null")
    terminalized_at = _optional_timestamp(raw.get("terminalized_at"), "terminalized_at")
    if status == "terminal" and (not reason_raw or terminalized_at is None):
        _invalid("terminal owner must carry its reason and terminalized time")
    if status != "terminal" and (reason_raw is not None or terminalized_at is not None):
        _invalid("only a terminal owner may carry terminal metadata")
    return terminalized_at, reason_raw


def _parse(key: CodingSessionKey, value: object) -> CodingSessionOwner:
    if not isinstance(value, dict):
        _invalid("owner root must be an object")
    raw = cast("dict[str, object]", value)
    if raw.get("schema_version") != SCHEMA_VERSION:
        _invalid(f"unknown schema_version {raw.get('schema_version')!r}")
    if raw.get("cluster") != key.cluster or raw.get("workspace") != key.workspace:
        _invalid("record key does not match its canonical path")
    if raw.get("tool") != key.tool:
        _invalid("record tool does not match its canonical path")
    status = raw.get("status")
    if status not in ("launching", "active", "terminal"):
        _invalid(f"unknown status {status!r}")
    persisted_status = status
    generation, owner_agent_id, label, suffix = _parse_generation_identity(key, raw)
    raw_state_dir = Path(_required_str(raw, "state_dir"))
    expected_state_dir = generation_state_dir(key, generation)
    if raw_state_dir.resolve() != expected_state_dir.resolve():
        _invalid("state_dir is outside this canonical generation")
    tasks_file = Path(_required_str(raw, "tasks_file"))
    work_file = Path(_required_str(raw, "work_file"))
    if not tasks_file.is_absolute() or not work_file.is_absolute():
        _invalid("task and work file paths must be absolute")
    session_id, session_name = _parse_session_identity(
        raw, persisted_status, owner_agent_id, suffix
    )
    supervisor_id, supervisor_name = _parse_supervisor_identity(
        key, generation, raw, persisted_status, owner_agent_id
    )
    terminalized_at, terminal_reason = _parse_terminal_metadata(raw, persisted_status)
    return CodingSessionOwner(
        key=key,
        status=persisted_status,
        generation=generation,
        owner_agent_id=owner_agent_id,
        display_label=label,
        expected_suffix=suffix,
        session_id=session_id,
        session_name=session_name,
        supervisor_session_id=supervisor_id,
        supervisor_session_name=supervisor_name,
        state_dir=expected_state_dir,
        tasks_file=tasks_file,
        work_file=work_file,
        created_at=_timestamp(raw.get("created_at"), "created_at"),
        expires_at=_timestamp(raw.get("expires_at"), "expires_at"),
        terminalized_at=terminalized_at,
        terminal_reason=terminal_reason,
    )


def read_unlocked(key: CodingSessionKey) -> CodingSessionOwner:
    try:
        value = json.loads(state_path(key).read_text(encoding="utf-8"))
        return _parse(key, value)
    except FileNotFoundError:
        return CodingSessionOwner(key=key, status="inactive")
    except (OSError, TypeError, ValueError) as exc:
        return CodingSessionOwner(key=key, status="invalid", error=str(exc))


def _payload(owner: CodingSessionOwner) -> dict[str, object]:
    return {
        "schema_version": SCHEMA_VERSION,
        "status": owner.status,
        "cluster": owner.key.cluster,
        "workspace": owner.key.workspace,
        "tool": owner.key.tool,
        "generation": owner.generation,
        "owner_agent_id": owner.owner_agent_id,
        "display_label": owner.display_label,
        "expected_suffix": owner.expected_suffix,
        "session_id": owner.session_id,
        "session_name": owner.session_name,
        "supervisor_session_id": owner.supervisor_session_id,
        "supervisor_session_name": owner.supervisor_session_name,
        "state_dir": str(owner.state_dir),
        "tasks_file": str(owner.tasks_file),
        "work_file": str(owner.work_file),
        "created_at": owner.created_at.isoformat() if owner.created_at else None,
        "expires_at": owner.expires_at.isoformat() if owner.expires_at else None,
        "terminalized_at": owner.terminalized_at.isoformat() if owner.terminalized_at else None,
        "terminal_reason": owner.terminal_reason,
    }


def _fsync_parent(path: Path) -> None:
    if os.name == "nt":
        return
    fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def write_unlocked(owner: CodingSessionOwner) -> None:
    path = state_path(owner.key)
    fd, raw_tmp = tempfile.mkstemp(dir=path.parent, prefix=".coding-owner-", suffix=".tmp")
    if os.name != "nt":
        os.fchmod(fd, 0o600)
    tmp = Path(raw_tmp)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(_payload(owner), stream, separators=(",", ":"), sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)  # noqa: PTH105 — generation publication commit point
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    with contextlib.suppress(OSError):
        _fsync_parent(path)
