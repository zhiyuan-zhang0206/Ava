"""Built-in schedules — the version-controlled schedules that ship with Ava.

Policy (user ruling 2026-08-11, pre-open-source): product schedules
(self-evolution, memory) are built in AND start by default; cluster-operator
schedules (e.g. trace-ship-tempo) are built in but start disabled. The manifest
lives at ``<repo>/schedules/manifest.json`` next to the schedule script
templates — it is the single expression of that policy.

``provision_builtin_schedules()`` does two things per manifest entry:

- **create** the schedule when its row is missing, with ``enabled`` taken from the
  manifest's ``default_enabled``;
- **resync** an existing row whose ``script`` / ``command`` differ from the repo
  template. The checkout is the source of truth for a built-in's code: the DB copy
  is a snapshot made at creation, and a snapshot goes stale when the library API
  it calls moves (2026-10-03: ``catch_up()`` gained a required ``db`` and thirteen
  stale snapshots crash-looped). The resync writes the template into the row,
  snapshots a ``schedule_versions`` row (note ``builtin-resync <hash>``) and queues
  a ``schedule_sync_requests`` row so a live session is relaunched onto the new code.

Everything else on an existing row — ``enabled``, ``description``, status — is
never touched, so stopping a built-in keeps it stopped. Delete a built-in and the
next provision brings it back with its default state. Agent-created schedules are
not in the manifest and are never read or written here. Both operations are
idempotent: a second provision with an unchanged checkout changes nothing.

Called from the gateway lifespan at boot (a fresh install comes up with its
built-ins) and from ``ava schedules provision`` (manual restore). Both call
sites run it against their own DB connection; the gateway's reconcile loop
launches any newly created enabled schedule within a poll tick.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

from base.paths import repo_root

# The manifest ships in the repo checkout every deployed unit runs from
# ($AVA_HOME/source on prod), so repo_root() resolves it in prod and in dev
# worktrees alike.
MANIFEST_PATH = repo_root() / "schedules" / "manifest.json"

# The two manifest classes — product (default enabled) and operator (default
# disabled). Unknown classes fail fast rather than guessing a default.
_BUILTIN_CLASSES = frozenset({"product", "operator"})


class ManifestError(ValueError):
    """The built-in schedules manifest is malformed — missing fields, an
    unknown class, or a script file that does not sit beside the manifest."""


@dataclass(frozen=True)
class BuiltinSchedule:
    """One manifest entry — a schedule that ships with Ava."""

    name: str
    klass: str  # "product" | "operator"
    default_enabled: bool
    description: str
    script: str
    command: str


def load_manifest(path: Path | None = None) -> list[BuiltinSchedule]:
    """Parse and validate the built-in schedules manifest.

    Raises:
        FileNotFoundError: no manifest at ``path`` (or the default).
        ManifestError: malformed manifest — missing fields, unknown class, or
            a script file that does not sit beside the manifest.
    """
    manifest_path = path or MANIFEST_PATH
    with manifest_path.open() as f:
        payload: Any = json.load(f)
    if not isinstance(payload, dict):
        raise ManifestError(f"malformed manifest {manifest_path}: expected a JSON object")
    raw = cast("dict[str, Any]", payload)
    entries_raw = raw.get("builtin_schedules")
    if not isinstance(entries_raw, list):
        raise ManifestError(
            f"malformed manifest {manifest_path}: expected {{builtin_schedules: [...]}}"
        )
    entries = cast("list[Any]", entries_raw)
    schedules: list[BuiltinSchedule] = []
    for item in entries:
        if not isinstance(item, dict):
            raise ManifestError(f"malformed manifest entry: {item!r} — expected an object")
        entry = cast("dict[str, Any]", item)
        name = entry.get("name")
        klass = entry.get("class")
        default_enabled = entry.get("default_enabled")
        description = entry.get("description", "")
        script = entry.get("script")
        command = entry.get("command")
        if not isinstance(name, str) or not name:
            raise ManifestError(f"malformed manifest entry: {entry!r} — missing 'name'")
        if klass not in _BUILTIN_CLASSES:
            raise ManifestError(
                f"manifest schedule {name!r}: unknown class {klass!r} "
                f"(expected {sorted(_BUILTIN_CLASSES)})"
            )
        if not isinstance(default_enabled, bool):
            raise ManifestError(f"manifest schedule {name!r}: 'default_enabled' must be a bool")
        if not isinstance(script, str) or not isinstance(command, str):
            raise ManifestError(f"manifest schedule {name!r}: 'script' and 'command' are required")
        script_file = manifest_path.parent / script
        if not script_file.is_file():
            raise ManifestError(
                f"manifest schedule {name!r}: script file {script_file} does not exist"
            )
        schedules.append(
            BuiltinSchedule(
                name=name,
                klass=klass,
                default_enabled=default_enabled,
                description=description,
                script=script,
                command=command,
            )
        )
    return schedules


def _no_names() -> list[str]:
    return []


@dataclass(frozen=True)
class ProvisionResult:
    """What one provision changed, in manifest order."""

    created: list[str] = field(default_factory=_no_names)
    resynced: list[str] = field(default_factory=_no_names)


def _digest(script: str, command: str) -> str:
    return hashlib.sha256(f"{command}\0{script}".encode()).hexdigest()[:12]


def provision_builtin_schedules(conn: Any, *, path: Path | None = None) -> ProvisionResult:
    """Create the manifest schedules that are missing and resync the ones that drifted.

    Idempotent. A row whose ``name`` exists keeps its ``enabled`` state and
    description; only its ``script`` and ``command`` follow the repo template, and
    only when they differ from it (compared by content hash).

    Args:
        conn: an open psycopg connection (autocommit or not; the caller owns
            the transaction).
        path: manifest path override (tests inject a fixture manifest).
    """
    manifest = load_manifest(path)
    manifest_path = (path or MANIFEST_PATH).parent
    result = ProvisionResult()
    for sched in manifest:
        # The manifest names a template file; the DB stores the script text
        # (the runner materializes it to $AVA_HOME/schedules/<id>/).
        script_text = (manifest_path / sched.script).read_text()
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, script, command FROM schedules WHERE name = %s FOR UPDATE",
                (sched.name,),
            )
            existing = cur.fetchone()
            if existing is None:
                cur.execute(
                    "INSERT INTO schedules (name, description, script, command, enabled) "
                    "VALUES (%s, %s, %s, %s, %s) RETURNING id",
                    (
                        sched.name,
                        sched.description,
                        script_text,
                        sched.command,
                        sched.default_enabled,
                    ),
                )
                row = cur.fetchone()
                assert row is not None  # noqa: S101 — INSERT ... RETURNING always yields a row
                # Same "initial" version snapshot the API create writes, so a
                # provisioned built-in carries the same roll-back history shape.
                cur.execute(
                    "INSERT INTO schedule_versions (schedule_id, script, command, note) "
                    "VALUES (%s, %s, %s, %s)",
                    (row[0], script_text, sched.command, "initial"),
                )
                result.created.append(sched.name)
                continue
            schedule_id, current_script, current_command = existing
            want = _digest(script_text, sched.command)
            if _digest(current_script or "", current_command or "") == want:
                continue
            cur.execute(
                "UPDATE schedules SET script = %s, command = %s, updated_at = now() WHERE id = %s",
                (script_text, sched.command, schedule_id),
            )
            cur.execute(
                "INSERT INTO schedule_versions (schedule_id, script, command, note) "
                "VALUES (%s, %s, %s, %s)",
                (schedule_id, script_text, sched.command, f"builtin-resync {want}"),
            )
            # Same request the API leaves on a script edit: the schedule-manager
            # kills a live session and relaunches it (when enabled) on the new code.
            cur.execute(
                "INSERT INTO schedule_sync_requests (schedule_id) VALUES (%s) "
                "ON CONFLICT (schedule_id) DO UPDATE SET requested_at = clock_timestamp()",
                (schedule_id,),
            )
        result.resynced.append(sched.name)
    return result
