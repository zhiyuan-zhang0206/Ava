"""`ava cluster release status` — read-only view of this home's release state.

There is no fleet journal yet (`cli/release_fleet/`, slice FC-7): for a
single host this reads exactly the two things that already exist — the
currently selected release (`cli.release_operator.current`) and, if one is
under way or was last run, the local unit journal
(`cli.release_transition.journal.read_operation`). Never writes, selects,
drains or dispatches; `read_operation` only reads and validates bytes.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from cli.release_operator.current import current_release
from cli.release_transition.journal import read_operation
from shared.verified_file import regular_bytes


def _active_operation_path(home: Path) -> Path | None:
    try:
        raw = regular_bytes(home / "updates" / "active").decode().strip()
    except FileNotFoundError:
        return None
    return Path(raw)


def _operation_body(home: Path, operation: str | None) -> dict[str, Any] | None:
    path = (
        home / "updates" / operation / "operation.json"
        if operation
        else _active_operation_path(home)
    )
    if path is None:
        return None
    if not path.exists():
        raise ValueError(f"no operation journal at {path}")
    journal = read_operation(path)
    return {
        "path": str(path),
        "id": str(journal.request.id),
        "kind": journal.request.kind,
        "phase": journal.phase,
        "direction": journal.direction,
        "terminal": journal.terminal,
        "attempt": journal.attempt,
        "launch_attempted": journal.launch_attempted,
        "error": journal.error,
    }


def _status_body(*, operation: str | None) -> dict[str, Any]:
    from shared.machine import machine_name
    from shared.paths import ava_home

    home = ava_home()
    found = current_release(home)
    return {
        "home": str(home),
        "machine": machine_name(),
        "current": None
        if found is None
        else {
            "artifact_digest": found[0].artifact_digest,
            "manifest_digest": found[0].manifest_digest,
            "schema_digest": found[0].schema_digest,
            "source_commit": found[0].source_commit,
        },
        "operation": _operation_body(home, operation),
    }


def _render(body: dict[str, Any]) -> str:
    lines = [f"home: {body['home']}", f"machine: {body['machine']}"]
    current = body["current"]
    if current is None:
        lines.append("current release: none (not yet adopted)")
    else:
        lines.append(
            f"current release: commit {current['source_commit']} "
            f"(artifact {current['artifact_digest'][:12]})"
        )
    operation = body["operation"]
    if operation is None:
        lines.append("release operation: none")
    else:
        lines.append(
            f"release operation {operation['id']}: phase={operation['phase']} "
            f"direction={operation['direction']} terminal={operation['terminal']} "
            f"attempt={operation['attempt']} error={operation['error']}"
        )
    return "\n".join(lines) + "\n"


def cmd_release_status(*, operation: str | None, as_json: bool) -> int:
    try:
        body = _status_body(operation=operation)
    except (ValueError, OSError, RuntimeError) as exc:
        sys.stderr.write(f"release status refused: {exc}\n")
        return 2
    sys.stdout.write(json.dumps(body, sort_keys=True) + "\n" if as_json else _render(body))
    return 0
