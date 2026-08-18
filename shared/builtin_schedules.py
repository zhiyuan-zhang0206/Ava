"""Built-in schedules — the version-controlled schedules that ship with Ava.

Policy (user ruling 2026-08-11, pre-open-source): product schedules
(self-evolution, memory) are built in AND start by default; cluster-operator
schedules (e.g. trace-ship-tempo) are built in but start disabled. The manifest
lives at ``<repo>/schedules/manifest.json`` next to the schedule script
templates — it is the single expression of that policy.

``provision_builtin_schedules()`` creates every manifest schedule missing from
the ``schedules`` table, with ``enabled`` taken from the manifest's
``default_enabled``. It is idempotent and non-destructive: an existing row is
never touched (not even re-enabled), so an operator's edits — script,
description, enabled — survive every provision. Delete a built-in and the next
provision brings it back with its default state; stop it (enabled=false) to
keep it around without running.

Called from the gateway lifespan at boot (a fresh install comes up with its
built-ins) and from ``ava schedules provision`` (manual restore). Both call
sites run it against their own DB connection; the gateway's reconcile loop
launches any newly created enabled schedule within a poll tick.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from shared.paths import repo_root

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


def provision_builtin_schedules(conn: Any, *, path: Path | None = None) -> list[str]:
    """Create every manifest schedule missing from the ``schedules`` table.

    Idempotent: a row whose ``name`` already exists is left untouched (its
    enabled state, script, and description are never overwritten). New rows are
    inserted with ``enabled = default_enabled`` per the manifest policy.

    Args:
        conn: an open psycopg connection (autocommit or not; the caller owns
            the transaction).
        path: manifest path override (tests inject a fixture manifest).

    Returns:
        The names of the schedules this call created, in manifest order.
    """
    manifest = load_manifest(path)
    manifest_path = (path or MANIFEST_PATH).parent
    created: list[str] = []
    for sched in manifest:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM schedules WHERE name = %s", (sched.name,))
            if cur.fetchone() is not None:
                continue
            # The manifest names a template file; the DB stores the script text
            # (the runner materializes it to $AVA_HOME/schedules/<id>/).
            script_text = (manifest_path / sched.script).read_text()
            cur.execute(
                "INSERT INTO schedules (name, description, script, command, enabled) "
                "VALUES (%s, %s, %s, %s, %s) RETURNING id",
                (sched.name, sched.description, script_text, sched.command, sched.default_enabled),
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
        created.append(sched.name)
    return created
