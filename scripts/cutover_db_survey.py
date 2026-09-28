"""Read-only survey of the gateway database records the new runtime reads (fleet cutover, FC-4).

The inventory half of `scripts/cutover_db_records.py`: one snapshot read of
every record the cutover repairs touch, the plan's D-series checks (Appendix A
of the fleet cutover plan, plus decoding with the current models), and the
classification of every `agents_meta` row whose `incarnation_resources` the
current model cannot decode. Nothing here writes. This one-time script reads
the retired writer's shapes on purpose; the runtime keeps no parser for them.
It is deleted after the cutover with the other `scripts/cutover_*` scripts.

A retired-shape row is classified with the conversion's own rule
(`shared.predecessor_closure.successor_refusal`), read only: `convertible` (a
named incarnation, a row its successor would take once converted, a settled
lifecycle receipt, a machine attestation proving every recorded identity gone),
`awaiting` (everything but the attestation), `inadmissible` with the reason (a
live or different incarnation, an unsettled command, no receipt, a paused
machine, a machine with no unit left to attest, an unattested identity, a
malformed value), or `unconvertible` with
the reason (a shape neither resurrection nor admission accepts even after
conversion). The conversion itself re-checks every guard under the row lock.
The last two are `FENCED`: the runtime keeps refusing those agents, so D-8
reads `repair` while convertible or awaiting rows remain, then `fenced` (never
`ok`) while any fenced row remains, with a count per verdict and reason.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass, field, fields
from typing import Any, LiteralString, cast
from uuid import UUID

import psycopg
from psycopg.pq import TransactionStatus
from psycopg.rows import dict_row
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from scripts.cutover_inventory import Attestation, own_checkout
from shared.incarnation_resources import ResourceShapeError, decode_resources
from shared.predecessor_closure import (
    SUCCESSOR_COLUMNS,
    SettledReceipt,
    SuccessorRow,
    successor_refusal,
)
from shared.resource_admission import PREDECESSOR_RECEIPT

LEASE_COLUMNS = (
    "phase",
    "kind",
    "holder",
    "acquired_at",
    "expires_at",
    "settle_hosts",
    "settle_note",
    "settle_started_at",
)
LEASE_JSON = "jsonb_build_object(" + ",".join(f"'{c}',{c}" for c in LEASE_COLUMNS) + ")"
FREE_LEASE: dict[str, Any] = dict.fromkeys(LEASE_COLUMNS) | {"phase": "stable"}
EVIDENCE = "SELECT managed_writer_evidence FROM deployment_state WHERE id=1"
_RECEIPT = (
    "SELECT i.id, i.kind, COALESCE(i.payload ? 'cutover_closure', false), i.payload "  # noqa: S608 -- constant SQL fragment
    "FROM inbound_messages i "
    f"JOIN agents_meta m ON m.id=i.agent_id WHERE {PREDECESSOR_RECEIPT} "
    "ORDER BY (i.id=m.lifecycle_command_id) DESC NULLS LAST, i.id LIMIT 1"
)
_ROWS = (
    f"SELECT id, machine, incarnation_resources, {SUCCESSOR_COLUMNS} FROM agents_meta "  # noqa: S608 -- constant columns
    "WHERE incarnation_resources IS NOT NULL ORDER BY id"
)
_SUCCESSOR_FIELDS = tuple(item.name for item in fields(SuccessorRow))
_POSTURES = (
    "SELECT h.machine, h.posture, COALESCE(h.updater_lease_expires_at > now(), false) "
    "AS updater_live, to_jsonb(h) AS image FROM host_deploy_state h ORDER BY 1"
)
_MACHINES = "SELECT name, role, paused_at, pause_reason, stopped_at, gateway_url FROM machines"
_NULLS = "SELECT machine, count(*) FROM agents_meta WHERE incarnation_resources IS NULL GROUP BY 1"
# Verdicts whose agents the runtime keeps refusing after the repair.
FENCED = ("inadmissible", "unconvertible")
_EXAMPLES = 5
_OWNER = (
    "SELECT r.rolname AS owner, r.oid = 10 AS owner_is_bootstrap FROM pg_database d "
    "JOIN pg_roles r ON r.oid = d.datdba WHERE d.datname = current_database()"
)
_ROLES = (
    "SELECT oid::bigint AS oid, rolname, rolcanlogin, rolsuper FROM pg_roles "
    "WHERE rolname LIKE 'ava%%' OR oid = 10 ORDER BY oid"
)
_PIN = "SELECT target_sha, last_known_good_sha, pending_known_good_sha FROM cluster_pin"
# Reported as-is for the operator's review (the plan's Appendix A).
_INFO: dict[str, LiteralString] = {
    "D-7": "SELECT machine, status, runtime_kind, count(*) FROM agents_meta "
    "WHERE status <> 'terminated' GROUP BY 1, 2, 3 ORDER BY 1, 2, 3",
    "D-9": "SELECT kind, status, applied_at IS NOT NULL AS applied, "
    "observed_at IS NOT NULL AS observed, count(*) FROM inbound_messages "
    "WHERE kind IN ('restart', 'terminate') AND NOT (status = 'done' AND observed_at IS NOT NULL) "
    "GROUP BY 1, 2, 3, 4 ORDER BY 1, 2, 3, 4",
    "D-12": "SELECT archived_count, last_archived_wal, last_archived_time, failed_count, "
    "last_failed_wal, last_failed_time FROM pg_stat_archiver",
    "D-13": "SELECT pg_size_pretty(pg_database_size(current_database())) AS size",
    "D-14": "SELECT count(*) AS unresolved FROM alerts WHERE source = 'deploy-probe' "
    "AND alertname = 'update failed: host left held' AND status = 'unresolved'",
}


class RetiredUnit(BaseModel):
    """One `machine_units` row the operator retires, with its evidence."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    machine: str = Field(min_length=1)
    home: str = Field(min_length=1)
    evidence: str = Field(min_length=1)


@dataclass(frozen=True)
class Inputs:
    """Operator-supplied decisions; nothing here is inferred from the database."""

    operator: str | None = None
    reason: str | None = None
    pending: Any = None  # the exact pending publication to clear; None = not supplied
    lease: dict[str, Any] | None = None  # the exact lease columns to release
    retire_units: tuple[RetiredUnit, ...] = ()
    attestations: dict[str, Attestation] = field(default_factory=dict[str, Attestation])
    raw: dict[str, bytes] = field(default_factory=dict[str, bytes])  # machine -> exact bytes
    legacy_commit: str | None = None

    @property
    def retired(self) -> set[tuple[str, str]]:
        return {(unit.machine, unit.home) for unit in self.retire_units}

    def digest(self, machine: str) -> str:
        return hashlib.sha256(self.raw[machine]).hexdigest()

    def record(self) -> dict[str, Any]:
        """What the journal keeps; a continuation of an incomplete run must match it."""
        return {
            "operator": self.operator,
            "reason": self.reason,
            "pending": self.pending,
            "lease": self.lease,
            "retire_units": [unit.model_dump() for unit in self.retire_units],
            "attestations": {machine: self.digest(machine) for machine in sorted(self.raw)},
        }


@dataclass
class Legacy:
    """One `agents_meta` row whose resources the current model cannot decode."""

    agent_id: int
    machine: str
    status: str
    before: Any
    closed: tuple[UUID, UUID] | None = None
    identities: list[dict[str, Any]] = field(default_factory=list[dict[str, Any]])
    receipt: int | None = None
    receipt_before: Any = None  # the receipt's payload, which the conversion also writes
    verdict: str = "convertible"
    reason: str | None = None

    def refuse(self, reason: str, verdict: str = "inadmissible") -> Legacy:
        self.verdict, self.reason = verdict, reason
        return self

    def summary(self) -> dict[str, Any]:
        """Everything but the before images (they are in the journal of a conversion)."""
        return {
            key: value
            for key, value in vars(self).items()
            if key not in ("before", "receipt_before")
        }


@dataclass
class Survey:
    """Every fact the check and the repairs need, read in one snapshot."""

    units: list[dict[str, Any]]
    machines: list[dict[str, Any]]
    postures: list[dict[str, Any]]
    evidence: Any
    lease: dict[str, Any]
    legacy: list[Legacy] = field(default_factory=list[Legacy])
    counts: dict[str, dict[str, int]] = field(default_factory=dict[str, dict[str, int]])
    checks: dict[str, dict[str, Any]] = field(default_factory=dict[str, dict[str, Any]])

    @property
    def paused(self) -> set[str]:
        return {row["name"] for row in self.machines if row["paused_at"] is not None}

    def included(self, retired: set[tuple[str, str]]) -> set[str]:
        """Machines with a unit that is neither paused nor being retired."""
        paused = self.paused
        return {
            unit["machine_name"]
            for unit in self.units
            if unit["machine_name"] not in paused
            and (unit["machine_name"], unit["home"]) not in retired
        }


def rows(conn: psycopg.Connection[Any], sql: LiteralString, *params: Any) -> list[dict[str, Any]]:
    with conn.cursor(row_factory=dict_row) as cur:
        return cur.execute(sql, params).fetchall()


def value(conn: psycopg.Connection[Any], sql: LiteralString, *params: Any) -> Any:
    row = conn.execute(sql, params).fetchone()
    if row is None:
        raise RuntimeError(f"no row for {sql}")
    return row[0]


def _mapping(raw: object) -> dict[str, object] | None:
    return cast("dict[str, object]", raw) if isinstance(raw, dict) else None


def pending_of(evidence: Any) -> Any:
    """The durable pending publication, None when clear (JSON null counts as clear)."""
    fields = _mapping(evidence)
    return None if fields is None else fields.get("pending")


def publication_problem(evidence: Any) -> str | None:
    """Why admission cannot read `evidence`; None when it can (SQL NULL is protocol zero)."""
    from shared.managed_writer_publication import WriterPublication

    if evidence is None:
        return None
    try:
        state = WriterPublication.model_validate_json(json.dumps(evidence))
    except ValidationError as exc:
        return f"does not decode with the current publication model: {exc.errors()[0]['msg']}"
    if state.pending is None and state.current is None:
        return "names neither a current nor a pending publication"
    return None


def cleared_publication(evidence: dict[str, Any]) -> dict[str, Any] | None:
    """The evidence without its pending record; SQL NULL when nothing was published."""
    return None if evidence.get("current") is None else evidence | {"pending": None}


def _migrations(conn: psycopg.Connection[Any], legacy_commit: str | None) -> dict[str, Any]:
    from shared.migration_layout import required_migration_set, required_migration_set_at_ref

    applied = {row[0] for row in conn.execute("SELECT name FROM schema_migrations")}
    current = required_migration_set()
    legacy = None
    if legacy_commit is not None:
        legacy = required_migration_set_at_ref(legacy_commit, repo_root=own_checkout())
    equals = [
        name for name, wanted in (("legacy", legacy), ("current", current)) if applied == wanted
    ]
    return {
        "verdict": "ok" if equals else "attention",
        "applied": len(applied),
        "latest": max(applied, default=None),
        "equals": equals,
        "unknown_to_this_checkout": sorted(applied - current),
        "pending_for_this_checkout": sorted(current - applied),
        "differs_from_legacy": None if legacy is None else sorted(applied ^ legacy),
    }


def _postures(found: Survey, retired: set[tuple[str, str]]) -> dict[str, Any]:
    included, verdict = found.included(retired), "ok"
    for row in found.postures:
        if row["machine"] not in included:
            continue
        if row["posture"] == "converging" or row["updater_live"]:
            verdict = "attention"
        elif row["posture"] == "paused" and verdict == "ok":
            verdict = "repair"
    return {"verdict": verdict, "rows": [row["image"] for row in found.postures]}


def _pin(conn: psycopg.Connection[Any], legacy_commit: str | None) -> dict[str, Any]:
    pin = rows(conn, _PIN)
    if legacy_commit is None:
        return {"verdict": "info", "pin": pin, "note": "pass --legacy-commit to compare"}
    matches = bool(pin) and pin[0]["target_sha"] == legacy_commit
    return {"verdict": "ok" if matches else "attention", "pin": pin, "legacy_commit": legacy_commit}


def fenced_summary(legacy: list[Legacy]) -> list[dict[str, Any]]:
    """The fenced rows per (verdict, reason): a count and the first agent ids."""
    groups: dict[tuple[str, str], list[int]] = {}
    for item in legacy:
        if item.verdict in FENCED:
            groups.setdefault((item.verdict, str(item.reason)), []).append(item.agent_id)
    return [
        {"verdict": verdict, "reason": reason, "count": len(agents), "agents": agents[:_EXAMPLES]}
        for (verdict, reason), agents in sorted(groups.items())
    ]


def _pinned(pin: list[dict[str, Any]]) -> str:
    return pin[0]["target_sha"] if pin and pin[0]["target_sha"] else "no commit"


# Why each check can read `attention`: a premise to resolve before any repair.
_WHY: dict[str, Callable[[dict[str, Any]], str]] = {
    "D-1": lambda check: f"admission cannot read the managed-writer evidence: {check['problem']}",
    "D-3": lambda check: (
        f"the cluster pin names {_pinned(check['pin'])}, not --legacy-commit "
        f"{check['legacy_commit']}"
    ),
    "D-4": lambda _check: (
        "the applied migration set equals neither --legacy-commit's nor this checkout's"
    ),
    "D-6": lambda _check: (
        "an included host carries a legacy updater (converging posture or live updater lease)"
    ),
    "D-10": lambda check: (
        f"the database is owned by the bootstrap superuser {check['database_owner']}"
    ),
    "D-11": lambda check: (
        f"{check['prepared']} prepared transaction(s) can hold the row locks the repairs take"
    ),
}


def attention(checks: dict[str, dict[str, Any]]) -> list[str]:
    """Every check reading `attention`, named with why; the repair refuses while any remains."""
    return [
        f"{name} reads attention: {_WHY[name](check)}"
        for name, check in checks.items()
        if check["verdict"] == "attention"
    ]


def _checks(conn: psycopg.Connection[Any], found: Survey, inputs: Inputs) -> dict[str, Any]:
    pending, problem = pending_of(found.evidence), publication_problem(found.evidence)
    owner = rows(conn, _OWNER)[0]
    prepared = value(conn, "SELECT count(*) FROM pg_prepared_xacts")
    unreconciled = [item for item in found.legacy if item.verdict in ("convertible", "awaiting")]
    fenced = fenced_summary(found.legacy)
    checks: dict[str, dict[str, Any]] = {
        "D-1": {
            "verdict": "repair" if pending is not None else ("attention" if problem else "ok"),
            "pending_clear": pending is None,
            "problem": problem,
            "pending": pending,
        },
        "D-2": {"verdict": "ok" if found.lease == FREE_LEASE else "repair", "lease": found.lease},
        "D-3": _pin(conn, inputs.legacy_commit),
        "D-4": _migrations(conn, inputs.legacy_commit),
        "D-5": {"verdict": "info", "units": found.units, "machines": found.machines},
        "D-6": _postures(found, inputs.retired),
        "D-8": {
            "verdict": "repair" if unreconciled else ("fenced" if fenced else "ok"),
            "counts": found.counts,
            "fenced": fenced,
            "rows": [item.summary() for item in found.legacy],
        },
        "D-10": {
            "verdict": "attention" if owner["owner_is_bootstrap"] else "ok",
            "database_owner": owner["owner"],
            "roles": rows(conn, _ROLES),
        },
        "D-11": {
            "verdict": "attention" if prepared else "ok",
            "max_prepared_transactions": value(conn, "SHOW max_prepared_transactions"),
            "prepared": prepared,
        },
    }
    for name, sql in _INFO.items():
        checks[name] = {"verdict": "info", "rows": rows(conn, sql)}
    return dict(sorted(checks.items(), key=lambda item: int(item[0][2:])))


def _process(receipt: object) -> tuple[int, float] | None:
    """A retired process receipt's `(pid, birth)`; None when it is not one."""
    fields = _mapping(receipt) or {}
    pid, birth = fields.get("pid"), fields.get("birth")
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        return None
    if isinstance(birth, bool) or not isinstance(birth, int | float) or birth <= 0:
        return None
    return pid, float(birth)


def _identities(resources: dict[str, object]) -> list[dict[str, Any]] | None:
    """Every recorded `(pid, birth)` of a retired value; None when one is malformed."""
    requests = _mapping(resources.get("requests"))
    if requests is None:
        return None
    slots: list[tuple[str, str | None, object]] = [("host", None, resources.get("host_process"))]
    for key, raw in requests.items():
        entry = _mapping(raw)
        if entry is None:
            return None
        slots += [(role, key, entry.get(f"{role}_process")) for role in ("owner", "root")]
    found: list[dict[str, Any]] = []
    for role, request, receipt in slots:
        if receipt is None:
            continue
        process = _process(receipt)
        if process is None:
            return None
        found.append({"role": role, "request": request, "pid": process[0], "birth": process[1]})
    return found


def _named_incarnation(resources: dict[str, object]) -> tuple[UUID, UUID] | None:
    """The `(generation, owner)` the retired value was admitted for."""
    if (resources.get("version"), resources.get("state")) != (1, "admitted"):
        return None
    generation, owner = resources.get("generation"), resources.get("owner")
    if not (isinstance(generation, str) and isinstance(owner, str)):
        return None
    try:
        return UUID(generation), UUID(owner)
    except ValueError:
        return None


def _settled(
    conn: psycopg.Connection[Any], legacy: Legacy, closed: tuple[UUID, UUID]
) -> SettledReceipt | None:
    """The predecessor receipt: the drain's applied restart held as the lifecycle
    pointer (preferred), else an applied and observed terminate."""
    found = conn.execute(_RECEIPT, (legacy.agent_id, *closed)).fetchone()
    if found is None:
        return None
    legacy.receipt, legacy.receipt_before = found[0], found[3]
    return SettledReceipt(*found[:3])


def _classify(conn: psycopg.Connection[Any], row: dict[str, Any], paused: set[str]) -> Legacy:
    """Static admissibility: `close_retired_predecessor`'s own guards, read-only."""
    legacy = Legacy(row["id"], row["machine"], row["status"], row["incarnation_resources"])
    resources = _mapping(legacy.before) or {}
    legacy.closed = _named_incarnation(resources)
    if legacy.closed is None:
        return legacy.refuse("the retired value names no admitted incarnation")
    identities = _identities(resources)
    if identities is None:
        return legacy.refuse("a recorded process identity is malformed")
    legacy.identities = identities
    receipt = _settled(conn, legacy, legacy.closed)
    shape = SuccessorRow(**{name: row[name] for name in _SUCCESSOR_FIELDS})
    refusal = successor_refusal(shape, legacy.closed, receipt)
    if refusal is not None:
        return legacy.refuse(refusal.reason, refusal.verdict)
    if row["machine"] in paused:
        return legacy.refuse("the machine is paused; no closure evidence exists for it")
    return legacy


def _weigh_evidence(legacy: Legacy, inputs: Inputs, attestable: set[str]) -> None:
    if legacy.verdict != "convertible":
        return
    if legacy.machine not in attestable:
        # An attestation must name a registered unit that is neither paused nor
        # retired (`_plan_evidence`, `_plan_units`), so none can ever cover it.
        legacy.refuse("no unit of its machine remains to attest (retired or unregistered)")
        return
    attestation = inputs.attestations.get(legacy.machine)
    if attestation is None:
        legacy.verdict, legacy.reason = "awaiting", "no closure attestation for this machine"
        return
    absent = attestation.absent()
    unproven = [item for item in legacy.identities if (item["pid"], item["birth"]) not in absent]
    if unproven:
        legacy.refuse(f"the attestation does not prove {len(unproven)} recorded identity(ies) gone")


def survey(conn: psycopg.Connection[Any], inputs: Inputs) -> Survey:
    """Read every record the repairs touch and classify the incarnation rows.

    The caller's transaction becomes one read-only REPEATABLE READ snapshot, so
    every check sees the same state and the survey cannot write.
    """
    if conn.info.transaction_status != TransactionStatus.INTRANS:
        raise RuntimeError("the survey runs inside the caller's transaction")
    conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
    found = Survey(
        units=[row["u"] for row in rows(conn, "SELECT to_jsonb(u) AS u FROM machine_units u")],
        machines=rows(conn, _MACHINES),
        postures=rows(conn, _POSTURES),
        evidence=value(conn, EVIDENCE),
        lease=value(conn, f"SELECT {LEASE_JSON} FROM deployment_state WHERE id=1"),  # noqa: S608 -- constant columns
    )
    found.units.sort(key=lambda unit: (unit["machine_name"], unit["home"]))
    paused, current = found.paused, dict[str, int]()
    for row in rows(conn, _ROWS):
        try:
            decode_resources(row["incarnation_resources"])
        except ResourceShapeError:
            found.legacy.append(_classify(conn, row, paused))
            continue
        current[row["machine"]] = current.get(row["machine"], 0) + 1
    attestable = found.included(inputs.retired)
    for legacy in found.legacy:
        _weigh_evidence(legacy, inputs, attestable)
    verdicts = ("convertible", "awaiting", "inadmissible", "unconvertible")
    found.counts = {
        "current_model": current,
        "null_protocol_zero": dict(conn.execute(_NULLS).fetchall()),
        "unconverted": {v: sum(1 for i in found.legacy if i.verdict == v) for v in verdicts},
    }
    found.checks = _checks(conn, found, inputs)
    return found


def export_rows(found: Survey) -> list[dict[str, Any]]:
    """Every recorded legacy identity, in the `cutover_inventory.py --attest` input shape."""
    return [
        {"machine": item.machine, "agent_id": item.agent_id, **identity}
        for item in found.legacy
        for identity in item.identities
    ]
