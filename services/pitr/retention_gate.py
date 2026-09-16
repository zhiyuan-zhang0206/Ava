"""Operator gate primitives for the retention deletion machine (#2150 P3).

The explicit `ava pitr retention` commands are the only sanctioned writers of
the arm carriers (design v0.3 section 3.2): `arm` records the operator's
approval of one plan digest, `disable` reverts to the default-off state, and
every flip lands in the append-only journal around the `.env` write. The
scheduler daemon re-reads the carriers from the unit `.env` on every tick, so
a flip takes effect on the next tick without a restart.

This module holds the state logic and the display formatters; the CLI wrappers
print and choose exit codes. The status command reads the daemon's in-memory
machine state through `shared.daemon_health.read_health_payload` (the same
home-identity contract as the probes) and degrades to file state when the
daemon does not answer.
"""

from __future__ import annotations

import getpass
import json
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

from services.pitr.retention_journal import RetentionJournal
from services.pitr.retention_manifest import RetentionPlan
from services.pitr.retention_planner import inspect_dry_run_plan
from shared import runtime_config
from shared.config import field_alias_map
from shared.paths import ava_home
from shared.turn_identity import effective_agent_id

ARMED_FIELD = "pitr_retention_delete_armed"
DIGEST_FIELD = "pitr_retention_delete_approved_digest"
DAEMON_NAME = "pitr_base_backup"
_RETENTION_COMPONENT = "pitr_retention_dry_run"


@dataclass(frozen=True)
class CarrierState:
    """The two arm carriers as they sit in the unit `.env` (None = unset)."""

    armed: bool | None
    approved_digest: str | None

    @classmethod
    def read(cls) -> CarrierState:
        raw = runtime_config.read_env_aliases()
        aliases = field_alias_map()
        armed_raw = raw.get(aliases[ARMED_FIELD])
        return cls(
            armed=(
                None
                if armed_raw is None
                else armed_raw.strip().lower() in {"1", "true", "yes", "on"}
            ),
            approved_digest=raw.get(aliases[DIGEST_FIELD]),
        )


def write_arm_carriers(digest: str) -> CarrierState:
    """Write the armed carrier and the approved digest (the sanctioned writer)."""
    runtime_config.write_fields({ARMED_FIELD: True, DIGEST_FIELD: digest}, set())
    return CarrierState.read()


def clear_arm_carriers() -> CarrierState:
    """Drop both carriers -- the disable path back to the default-off state."""
    runtime_config.write_fields({}, {ARMED_FIELD, DIGEST_FIELD})
    return CarrierState.read()


def _gate_journal() -> RetentionJournal:
    return RetentionJournal(ava_home() / "physical-backup" / "retention-journal")


def _carrier_fields(state: CarrierState) -> dict[str, object]:
    return {"armed": state.armed, "approved_digest": state.approved_digest}


def append_gate_record(
    event: str,
    *,
    phase: str,
    plan_digest: str | None,
    before: CarrierState | None = None,
    after: CarrierState | None = None,
    extra: dict[str, object] | None = None,
) -> None:
    """Append one gate record around an arm/disable/run-once action.

    ``phase=intent`` carries the state the action is about to change
    (``before``); ``phase=applied``/``result`` carries the state it produced
    (``after``) -- a crash between the two is visible as an intent without its
    applied twin (design v0.3 section 4, the gate audit entries).
    """
    fields: dict[str, object] = {
        "event": event,
        "phase": phase,
        "actor_agent_id": effective_agent_id(),
        "actor_user": getpass.getuser(),
        "command": " ".join(sys.argv),
        "plan_digest": plan_digest,
    }
    if before is not None:
        fields["before"] = _carrier_fields(before)
    if after is not None:
        fields["after"] = _carrier_fields(after)
    if extra is not None:
        fields.update(extra)
    _gate_journal().append("gate", fields)


@dataclass(frozen=True)
class PlanSummary:
    """The display projection of the stored dry-run plan."""

    path: Path
    digest: str
    blocked_reasons: tuple[str, ...]
    eligible_objects: int
    eligible_bytes: int
    logical_eligible_objects: int
    logical_retained_objects: int
    weak_evidence_objects: int
    retained_objects: int
    retained_bytes: int
    orphan_sidecars: int
    mtime: float


def read_plan() -> PlanSummary | None:
    """The stored dry-run plan's summary, or None when none is on disk."""
    path = ava_home() / "physical-backup" / "retention-plans" / "latest.dry-run.json"
    if not path.is_file():
        return None
    plan: RetentionPlan = inspect_dry_run_plan(ava_home() / "physical-backup")
    return PlanSummary(
        path=path,
        digest=plan.digest(),
        blocked_reasons=plan.blocked_reasons,
        eligible_objects=len(plan.eligible),
        eligible_bytes=plan.eligible_bytes,
        logical_eligible_objects=sum(1 for item in plan.eligible if item.object.kind == "logical"),
        logical_retained_objects=sum(1 for item in plan.retained if item.object.kind == "logical"),
        weak_evidence_objects=len(plan.weak_evidence),
        retained_objects=len(plan.retained),
        retained_bytes=plan.retained_bytes,
        orphan_sidecars=len(plan.orphan_sidecars),
        mtime=path.stat().st_mtime,
    )


def display_armed(*, armed: bool | None) -> str:
    return "unset" if armed is None else ("true" if armed else "false")


def human_bytes(value: int) -> str:
    if value >= 1024**3:
        return f"{value / 1024**3:.1f} GiB"
    if value >= 1024**2:
        return f"{value / 1024**2:.1f} MiB"
    if value >= 1024:
        return f"{value / 1024:.1f} KiB"
    return f"{value} B"


def plan_line(plan: PlanSummary) -> str:
    blocked = "no" if not plan.blocked_reasons else f"YES ({'; '.join(plan.blocked_reasons)})"
    return (
        f"digest={plan.digest} blocked={blocked} "
        f"eligible={plan.eligible_objects} ({human_bytes(plan.eligible_bytes)}, "
        f"{plan.logical_eligible_objects} logical) "
        f"retained={plan.retained_objects} orphans={plan.orphan_sidecars} "
        f"weak-evidence={plan.weak_evidence_objects}"
    )


def iso_timestamp(value: object) -> str:
    if not isinstance(value, (int, float)):
        return "-"
    return datetime.fromtimestamp(value, tz=UTC).strftime("%Y-%m-%d %H:%M:%SZ")


def read_daemon_record() -> dict[str, object] | None:
    """The scheduler's retention health record, or None when it does not answer."""
    from shared.daemon_health import read_health_payload

    payload = read_health_payload(DAEMON_NAME, timeout_s=2.0)
    if payload is None:
        return None
    components = payload.get("components")
    if not isinstance(components, list):
        return None
    for record in cast("list[object]", components):
        if not isinstance(record, dict):
            continue
        candidate = cast("dict[str, object]", record)
        if candidate.get("name") == _RETENTION_COMPONENT:
            return candidate
    return None


def journal_tail(limit: int = 3) -> list[dict[str, object]]:
    path = ava_home() / "physical-backup" / "retention-journal" / "journal.jsonl"
    if not path.is_file():
        return []
    records: list[dict[str, object]] = []
    for line in path.read_text().splitlines()[-limit:]:
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            records.append(cast("dict[str, object]", parsed))
    return records


def journal_line(record: dict[str, object]) -> str:
    parts = [
        str(record.get("at", "")),
        str(record.get("kind", "")),
        str(record.get("event", "")),
        str(record.get("phase", "")),
    ]
    digest = record.get("plan_digest")
    if isinstance(digest, str):
        parts.append(f"digest={digest[:12]}")
    return " ".join(part for part in parts if part)
