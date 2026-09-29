"""`ava cluster release status` — read-only view of this home's release state.

It reads the currently selected release (`cli.release_operator.current`; one
that no longer verifies is reported as unverifiable, with the reason), the
cluster release state a completed fleet operation published
(`releases/fleet-state.json`, gateway homes only) and, if one is under way or
was last run, this home's operation journal: on the gateway the fleet journal
(phase, decisions, verdicts, every unit's inclusion and last instruction and
answer, alerts and their deliveries), on a remote unit its unit journal. Never
writes, selects, drains or dispatches; `read_operation` only reads and
validates bytes, and no lock is taken.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from cli.release_fleet.policy import AlertRoute
from cli.release_fleet.progress import FleetProgress, UnitProgress
from cli.release_fleet.request import FleetRequest
from cli.release_operator.current import current_release
from cli.release_transition.journal import Operation, read_operation
from shared.deploy.release.verified_file import regular_bytes


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
        "fleet": None if journal.fleet is None else _fleet(journal.fleet, _route(journal)),
        "unit": None if journal.unit is None else _unit(journal.unit),
    }


def _route(operation: Operation) -> AlertRoute:
    request = operation.request
    if not isinstance(request, FleetRequest):
        raise TypeError("fleet progress belongs to a fleet request")
    return request.policy.alert_route


def _fleet(progress: FleetProgress, route: AlertRoute) -> dict[str, Any]:
    return {
        "outcome": progress.outcome,
        "maintenance_at": progress.maintenance_at.isoformat(),
        "admitted_generation": None if progress.admitted is None else progress.admitted.number,
        "cohort": None if progress.cohort is None else progress.cohort.size,
        "decisions": [
            {"kind": d.kind, "phase": d.phase, "reason": d.reason, "at": d.at.isoformat()}
            for d in progress.decisions
        ],
        "verdicts": [
            {
                "stage": v.stage,
                "direction": v.direction,
                "action": v.action,
                "outcome": v.outcome,
                "affected": len(v.affected),
                "cohort": v.cohort_size,
            }
            for v in progress.verdicts
        ],
        "units": [
            {
                "unit": s.unit.label,
                "inclusion": s.inclusion,
                "reason": s.reason,
                "instruction": None if s.instruction is None else s.instruction.action,
                "sequence": None if s.instruction is None else s.instruction.sequence,
                "answered": s.answered,
            }
            for s in progress.units
        ],
        "alerts": [
            {
                "kind": r.alert.kind,
                "unit": None if r.alert.unit is None else r.alert.unit.label,
                "delivered": list(r.delivered),
                "undelivered": list(r.undelivered(route)),
            }
            for r in progress.alerts
        ],
    }


def _unit(progress: UnitProgress) -> dict[str, Any]:
    instruction = progress.instruction
    return {
        "outcome": progress.outcome,
        "instruction": None if instruction is None else instruction.action,
        "sequence": None if instruction is None else instruction.sequence,
        "acted": len(progress.acted),
        "answered": None if progress.report is None else progress.report.state,
    }


def _published(home: Path) -> dict[str, Any] | None:
    from cli.release_fleet.gateway import read_state

    state = read_state(home)
    if state is None:
        return None
    return {
        "operation": str(state.operation),
        "current": state.current.source_commit,
        "last_known_good": None
        if state.last_known_good is None
        else state.last_known_good.source_commit,
        "stale_units": [unit.label for unit in state.stale_units],
        "rejected": [r.release.source_commit for r in state.rejected],
    }


def _current(home: Path) -> dict[str, str] | None:
    """The verified selection, or why it no longer verifies: a corrupted or
    malformed image must not hide the operation journal from the operator."""
    try:
        found = current_release(home)
    except (ValueError, OSError, RuntimeError, KeyError) as exc:
        return {"unverifiable": f"{type(exc).__name__}: {exc}"}
    if found is None:
        return None
    return {
        "artifact_digest": found[0].artifact_digest,
        "manifest_digest": found[0].manifest_digest,
        "schema_digest": found[0].schema_digest,
        "source_commit": found[0].source_commit,
    }


def _status_body(*, operation: str | None) -> dict[str, Any]:
    from shared.machine import machine_name
    from shared.paths import ava_home

    home = ava_home()
    return {
        "home": str(home),
        "machine": machine_name(),
        "current": _current(home),
        "published": _published(home),
        "operation": _operation_body(home, operation),
    }


def _render(body: dict[str, Any]) -> str:
    lines = [f"home: {body['home']}", f"machine: {body['machine']}"]
    current = body["current"]
    if current is None:
        lines.append("current release: none (not yet adopted)")
    elif "unverifiable" in current:
        lines.append(f"current release: unverifiable ({current['unverifiable']})")
    else:
        lines.append(
            f"current release: commit {current['source_commit']} "
            f"(artifact {current['artifact_digest'][:12]})"
        )
    published = body["published"]
    if published is not None:
        lines.append(
            f"fleet release: commit {published['current']} "
            f"(known-good {published['last_known_good']}, stale units {published['stale_units']})"
        )
    operation = body["operation"]
    if operation is None:
        lines.append("release operation: none")
        return "\n".join(lines) + "\n"
    lines.append(
        f"{operation['kind']} operation {operation['id']}: phase={operation['phase']} "
        f"direction={operation['direction']} terminal={operation['terminal']} "
        f"attempt={operation['attempt']} error={operation['error']}"
    )
    fleet = operation["fleet"]
    if fleet is not None:
        lines.append(f"  outcome={fleet['outcome']} decisions={fleet['decisions']}")
        lines.extend(
            f"  alert {a['kind']} undelivered to {', '.join(a['undelivered'])}"
            for a in fleet["alerts"]
            if a["undelivered"]
        )
        lines.extend(
            f"  unit {u['unit']}: {u['inclusion']} instruction={u['instruction']} "
            f"answered={u['answered']} reason={u['reason']}"
            for u in fleet["units"]
        )
    unit = operation["unit"]
    if unit is not None:
        lines.append(
            f"  instruction={unit['instruction']} answered={unit['answered']} "
            f"outcome={unit['outcome']}"
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
