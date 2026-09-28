#!/usr/bin/env python3
"""One-time repair of the gateway database records the new runtime reads (fleet cutover, FC-4).

The retired runtime left records the new code refuses or misreads: process
receipts without boot scope in `agents_meta.incarnation_resources`, possibly a
durable pending publication, a legacy deploy-lease holder, `paused` host
postures, `machine_units` rows of homes that no longer exist, and terminated
rows without a runtime identity. No runtime path repairs them
(future/infra/unified-cluster-lifecycle.md). This script is the one explicit
repair; it is deleted after the cutover with the other `scripts/cutover_*`
scripts.

`--check` is read-only (`scripts/cutover_db_survey.py`): the plan's D-series
inventory, plus the refusals a repair would meet; `--rows-out` exports every
recorded legacy `(pid, birth)` for `scripts/cutover_inventory.py --attest`.
Without `--check` the script dry-runs the repairs; `--execute` applies them.
Every refusal is decided before a run's first write. The steps (`STEPS`), in
order: `pending`, `lease`, `posture`, `units`, `incarnations` (retired-shape
rows to the closed-predecessor form, `shared.predecessor_closure`) and
`identities` (a minted hosted identity for identity-less terminated rows, why:
decisions/2026-09-28-legacy-terminated-agents-resurrectable-at-cutover.md).
Each effect compares its row with the before and after images recorded at
planning. The journal, `$AVA_HOME/cutover-rollback/db-records/journal.json`
(0600), records every planned effect with its before image before the first
write, and each result after it. What each step changes, the evidence it
requires, the rows left fenced and the journal's rules:
conventions/cutover-db-records.md.

Run on the gateway, from a checkout of the cutover commit:

    .venv/bin/python scripts/cutover_db_records.py --home ~/.ava --check [--rows-out R.json]
    .venv/bin/python scripts/cutover_db_records.py --home ~/.ava --attestation A.json ... \\
        --operator NAME --reason TEXT [--pending-json P] [--lease-json L] [--retire-units U]
    (the same) --execute
"""

from __future__ import annotations

import argparse
import getpass
import json
import subprocess
import sys
from collections.abc import Callable, Generator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, LiteralString, cast
from uuid import uuid4

import psycopg
from psycopg.types.json import Jsonb
from pydantic import TypeAdapter, ValidationError

from scripts.cutover_db_survey import (
    EVIDENCE,
    FREE_LEASE,
    IDENTITY_IMAGE,
    IDENTITYLESS,
    LEASE_COLUMNS,
    LEASE_JSON,
    POINTER_CLEARS,
    Inputs,
    RetiredUnit,
    Survey,
    attention,
    cleared_publication,
    clock_refusal,
    export_rows,
    fenced_summary,
    pending_of,
    publication_problem,
    survey,
    value,
)
from scripts.cutover_inventory import (
    ARCHIVE,
    Attestation,
    RefusedError,
    canonical_home,
    load_attestation,
    own_checkout,
    registry_path,
)
from scripts.cutover_inventory import read_journal as read_adoption
from shared.incarnation_resources import IncarnationResources, ResourceEvidenceError
from shared.pg_admin import OwnerAuthority
from shared.predecessor_closure import ClosureEvidence, close_retired_predecessor
from shared.private_storage import ensure_private_dir, write_private_bytes

VERSION = 2  # 2: each run names the adoption it belongs to
RECORD = f"{ARCHIVE}/db-records"
JOURNAL = f"{RECORD}/journal.json"
STEPS = ("pending", "lease", "posture", "units", "incarnations", "identities")
# Ceilings of the owner session. A leftover transaction holding a row a repair
# locks (a prepared one survives restarts; D-11) fails that effect instead of
# hanging the run.
LOCK_TIMEOUT = "10s"
STATEMENT_TIMEOUT = "60s"
_UNITS = TypeAdapter(tuple[RetiredUnit, ...])


@dataclass
class Plan:
    """Every planned effect, with its before image, and every refusal."""

    effects: dict[str, list[dict[str, Any]]]
    refusals: list[str]

    def refuse(self, reason: str) -> None:
        self.refusals.append(reason)


def _plan_pending(found: Survey, inputs: Inputs, plan: Plan) -> None:
    pending = pending_of(found.evidence)
    if pending is None:
        if inputs.pending is not None:
            plan.refuse("--pending-json names a pending publication; none is recorded")
        return
    if inputs.pending is None:
        plan.refuse(
            "a pending publication is recorded; supply its exact JSON (D-1) as --pending-json"
        )
        return
    if inputs.pending != pending:
        plan.refuse("--pending-json differs from the recorded pending publication")
        return
    after = cleared_publication(found.evidence)
    problem = publication_problem(after)
    if problem is not None:
        plan.refuse(f"clearing pending leaves evidence the runtime cannot read: {problem}")
        return
    plan.effects["pending"].append(
        {"op": "pending-clear", "before": found.evidence, "after": after}
    )


def _plan_lease(found: Survey, inputs: Inputs, plan: Plan) -> None:
    if found.lease == FREE_LEASE:
        if inputs.lease is not None:
            plan.refuse("--lease-json names a deploy lease; the lease is free")
    elif inputs.lease is None:
        plan.refuse(
            f"a legacy deploy lease is held ({found.lease['holder']}); supply its exact columns "
            "(D-2) as --lease-json"
        )
    elif inputs.lease != found.lease:
        plan.refuse("--lease-json differs from the recorded deploy lease")
    else:
        plan.effects["lease"].append(
            {"op": "lease-release", "before": found.lease, "after": FREE_LEASE}
        )


def _plan_postures(found: Survey, inputs: Inputs, plan: Plan, *, later: bool) -> None:
    included = found.included(inputs.retired)
    for row in found.postures:
        if row["machine"] not in included:
            continue
        if row["posture"] == "converging" or row["updater_live"]:
            plan.refuse(
                f"host {row['machine']} carries a legacy updater (posture {row['posture']}, "
                f"live updater lease {row['updater_live']}); resolve it by hand"
            )
        elif row["posture"] == "paused" and not later:
            plan.effects["posture"].append(
                {"op": "posture-idle", "machine": row["machine"], "before": row["image"]}
            )


def _plan_units(found: Survey, inputs: Inputs, plan: Plan, own: tuple[str, str]) -> None:
    registered = {(unit["machine_name"], unit["home"]): unit for unit in found.units}
    attested = {(doc.machine, doc.home) for doc in inputs.attestations.values()}
    for unit in inputs.retire_units:
        key, name = (unit.machine, unit.home), f"--retire-units: {unit.machine}:{unit.home}"
        if key not in registered:
            plan.refuse(f"{name} is not a registered unit")
        elif unit.machine in found.paused:
            plan.refuse(f"{name} belongs to a paused machine; its units are kept")
        elif key == own:
            plan.refuse(f"{name} is this gateway's own unit")
        elif key in attested:
            plan.refuse(f"{name} is attested as a live home")
        else:
            plan.effects["units"].append(
                {"op": "unit-retire", **unit.model_dump(), "before": registered[key]}
            )


def _plan_evidence(found: Survey, inputs: Inputs, plan: Plan) -> None:
    units = {(unit["machine_name"], unit["home"]) for unit in found.units}
    for machine, doc in sorted(inputs.attestations.items()):
        if (machine, doc.home) not in units:
            plan.refuse(f"the attestation for {machine} names {doc.home}, not a unit of it")
        elif not doc.proves_closure:
            plan.refuse(
                f"the attestation for {machine} does not prove closure "
                f"(all_absent {doc.all_absent}, census_empty {doc.census_empty})"
            )
    cluster_wide = plan.effects["pending"] or plan.effects["lease"] or plan.effects["posture"]
    missing = sorted(found.included(inputs.retired) - set(inputs.attestations))
    if cluster_wide and missing:
        plan.refuse(
            "the pending, lease and posture repairs need a closure attestation from every "
            f"included machine; missing: {', '.join(missing)}"
        )


def _plan_incarnations(found: Survey, inputs: Inputs, plan: Plan) -> None:
    for item in found.legacy:
        if item.verdict != "convertible" or item.closed is None:
            continue
        generation, owner = item.closed
        after = IncarnationResources(generation=generation, owner=owner, requests={})
        plan.effects["incarnations"].append(
            {
                "op": "predecessor-close",
                "agent_id": item.agent_id,
                "machine": item.machine,
                "receipt": item.receipt,
                "attestation_sha256": inputs.digest(item.machine),
                "before": item.before,
                "receipt_before": item.receipt_before,
                "after": after.model_dump(mode="json"),
            }
        )


def _plan_identities(found: Survey, inputs: Inputs, plan: Plan) -> None:
    """One effect per machine; each row's generation and owner are minted here,
    once, so a continuation of the run writes the recorded values."""
    rows: dict[str, list[dict[str, Any]]] = {}
    for item in found.identityless:
        if item.category == "convertible":
            minted = {"generation": str(uuid4()), "owner": str(uuid4())}
            rows.setdefault(item.machine, []).append(
                {"agent_id": item.agent_id, "before": item.before, **minted}
            )
    for machine, planned in sorted(rows.items()):
        plan.effects["identities"].append(
            {
                "op": "identity-mint",
                "machine": machine,
                "attestation_sha256": inputs.digest(machine),
                "count": len(planned),
                "rows": planned,
            }
        )


def plan_repairs(found: Survey, inputs: Inputs, own: tuple[str, str], *, later: bool) -> Plan:
    """Every effect with its before image, and every refusal, before any write.

    A check reading `attention` is a premise to resolve first (a legacy pin or
    migration set, a bootstrap-owned database, prepared transactions, a legacy
    updater): each refuses, named with why. D-8 `fenced` does not refuse; the
    run states and records which agents stay fenced instead. A run after a
    completed one (`later`) plans no posture effect: the completed run idled
    every legacy `paused` posture of an included host, so a later `paused` is a
    held unit's own (a start inside a hold writes it), which never changes here.
    The first run also refuses while a row reads `after_attestation` (`clock_refusal`).
    """
    plan = Plan({step: [] for step in STEPS}, [])
    for reason in attention(found.checks):
        plan.refuse(reason)
    if not later and (skew := clock_refusal(found)) is not None:
        plan.refuse(skew)
    _plan_pending(found, inputs, plan)
    _plan_lease(found, inputs, plan)
    _plan_postures(found, inputs, plan, later=later)
    _plan_units(found, inputs, plan, own)
    _plan_evidence(found, inputs, plan)
    _plan_incarnations(found, inputs, plan)
    _plan_identities(found, inputs, plan)
    return plan


# Each handler runs inside the effect's own transaction, reads its row under
# lock and returns "applied", "already" (a crashed run's committed effect) or,
# for a conversion the closure guards refuse, "refused: <why>". A record that
# is neither the before nor the after image stops the run.
def _changed(effect: dict[str, Any]) -> RuntimeError:
    return RuntimeError(f"{effect['op']}: the record changed since this run recorded it")


def _locked(conn: psycopg.Connection[Any], sql: LiteralString, *params: Any) -> Any:
    row = conn.execute(sql + " FOR UPDATE", params).fetchone()
    return None if row is None else row[0]


def _clear_pending(conn: psycopg.Connection[Any], effect: dict[str, Any], _inputs: Inputs) -> str:
    current = _locked(conn, EVIDENCE)
    if current == effect["after"]:
        return "already"
    if current != effect["before"]:
        raise _changed(effect)
    after = None if effect["after"] is None else Jsonb(effect["after"])
    conn.execute("UPDATE deployment_state SET managed_writer_evidence=%s WHERE id=1", (after,))
    return "applied"


def _release_lease(conn: psycopg.Connection[Any], effect: dict[str, Any], _inputs: Inputs) -> str:
    current = _locked(conn, f"SELECT {LEASE_JSON} FROM deployment_state WHERE id=1")  # noqa: S608 -- constant columns
    if current == FREE_LEASE:
        return "already"
    if current != effect["before"] or pending_of(value(conn, EVIDENCE)) is not None:
        raise _changed(effect)
    freed = ", ".join(f"{column}=NULL" for column in LEASE_COLUMNS if column != "phase")
    conn.execute(f"UPDATE deployment_state SET phase='stable', {freed} WHERE id=1")  # noqa: S608 -- constant columns
    return "applied"


def _idle_posture(conn: psycopg.Connection[Any], effect: dict[str, Any], _inputs: Inputs) -> str:
    current = _locked(
        conn, "SELECT to_jsonb(h) FROM host_deploy_state h WHERE machine=%s", effect["machine"]
    )

    def kept(image: dict[str, Any]) -> dict[str, Any]:
        return {k: v for k, v in image.items() if k not in {"posture", "paused_at", "updated_at"}}

    if current and current["posture"] == "idle" and kept(current) == kept(effect["before"]):
        return "already"
    if current != effect["before"]:
        raise _changed(effect)
    conn.execute(
        "UPDATE host_deploy_state SET posture='idle', paused_at=NULL, updated_at=now() "
        "WHERE machine=%s",
        (effect["machine"],),
    )
    return "applied"


def _retire_unit(conn: psycopg.Connection[Any], effect: dict[str, Any], _inputs: Inputs) -> str:
    key = (effect["machine"], effect["home"])
    sql = "SELECT to_jsonb(u) FROM machine_units u WHERE machine_name=%s AND home=%s"
    current = _locked(conn, sql, *key)
    if current is None:
        return "already"
    if current != effect["before"]:
        raise _changed(effect)
    conn.execute("DELETE FROM machine_units WHERE machine_name=%s AND home=%s", key)
    return "applied"


def _close_predecessor(
    conn: psycopg.Connection[Any], effect: dict[str, Any], inputs: Inputs
) -> str:
    aid, receipt = effect["agent_id"], effect["receipt"]
    current = value(conn, "SELECT incarnation_resources FROM agents_meta WHERE id=%s", aid)
    closure = value(
        conn, "SELECT payload->'cutover_closure' FROM inbound_messages WHERE id=%s", receipt
    )
    ours = {"before": effect["before"], "attestation_sha256": effect["attestation_sha256"]}
    if current == effect["after"] and closure and {key: closure[key] for key in ours} == ours:
        return "already"
    # The conversion also writes the receipt's payload: compare its before image
    # under the locks the conversion takes, metadata row first.
    _locked(conn, "SELECT id FROM agents_meta WHERE id=%s", aid)
    if (
        _locked(conn, "SELECT payload FROM inbound_messages WHERE id=%s", receipt)
        != effect["receipt_before"]
    ):
        raise _changed(effect)
    evidence = ClosureEvidence(
        machine=effect["machine"],
        attestation_sha256=effect["attestation_sha256"],
        operator=cast("str", inputs.operator),
        reason=cast("str", inputs.reason),
    )
    try:
        with conn.transaction():
            close_retired_predecessor(
                conn, aid, before=effect["before"], receipt=receipt, evidence=evidence
            )
    except ResourceEvidenceError as exc:
        return f"refused: {exc}"
    return "applied"


# The compare-and-swap restates every identity-less condition and the before image.
_MINT: LiteralString = (
    "UPDATE agents_meta m SET runtime_kind='hosted', runtime_generation=v.generation, "  # noqa: S608 -- constant fragments
    "runtime_owner=v.owner, pid=NULL FROM jsonb_to_recordset(%s) AS "
    "v(agent_id bigint, before jsonb, generation uuid, owner uuid) "
    f"WHERE m.id=v.agent_id AND m.machine=%s AND {IDENTITYLESS} AND {POINTER_CLEARS} "
    f"AND {IDENTITY_IMAGE}=v.before RETURNING m.id"
)


def _minted_image(row: dict[str, Any]) -> dict[str, Any]:
    identity = {"runtime_generation": row["generation"], "runtime_owner": row["owner"]}
    return row["before"] | identity | {"runtime_kind": "hosted", "pid": None}


def _unminted(conn: psycopg.Connection[Any], skipped: list[dict[str, Any]]) -> list[int]:
    """The rows the mint skipped that do not carry their minted pair either."""
    if not skipped:
        return []
    sql = f"SELECT m.id, {IDENTITY_IMAGE} FROM agents_meta m WHERE m.id=ANY(%s)"  # noqa: S608 -- constant fragment
    current = dict(conn.execute(sql, ([row["agent_id"] for row in skipped],)).fetchall())
    return [
        row["agent_id"] for row in skipped if current.get(row["agent_id"]) != _minted_image(row)
    ]


def _mint_identities(conn: psycopg.Connection[Any], effect: dict[str, Any], _inputs: Inputs) -> str:
    """Mint each row whose image is still its before image; a row that already
    carries its minted pair is `already`, any other row is left unchanged."""
    rows = effect["rows"]
    minted = {row[0] for row in conn.execute(_MINT, (Jsonb(rows), effect["machine"]))}
    changed = _unminted(conn, [row for row in rows if row["agent_id"] not in minted])
    if not changed:
        return "applied" if minted else "already"
    left = f"left {len(changed)} changed row(s) unchanged (agents {', '.join(map(str, changed))})"
    return f"applied: minted {len(minted)}, {left}" if minted else f"refused: {left}"


Handler = Callable[[psycopg.Connection[Any], dict[str, Any], Inputs], str]
_HANDLERS: dict[str, Handler] = {
    "pending-clear": _clear_pending,
    "lease-release": _release_lease,
    "posture-idle": _idle_posture,
    "unit-retire": _retire_unit,
    "predecessor-close": _close_predecessor,
    "identity-mint": _mint_identities,
}


def completed(journal: dict[str, Any] | None) -> bool:
    """Whether the journal records a completed run; every run after it is a late one."""
    return journal is not None and any(run["state"] == "done" for run in journal["runs"])


def adoption(home: Path) -> dict[str, str] | None:
    """The adoption a run belongs to: its journal's id and creation time. A rollback
    (R1, also run by R2) moves that journal aside, and a retry adopts anew."""
    found = read_adoption(home)
    return None if found is None else {k: found[k] for k in ("cutover_id", "created_at")}


def read_journal(home: Path) -> dict[str, Any] | None:
    """The journal; refuses one whose last run belongs to another adoption of the home."""
    from shared.verified_file import regular_bytes

    path = home / JOURNAL
    try:
        data: object = json.loads(regular_bytes(path))
    except FileNotFoundError:
        return None
    journal = cast("dict[str, Any]", data) if isinstance(data, dict) else {}
    if set(journal) != {"version", "home", "runs"} or journal["version"] != VERSION:
        raise RefusedError(f"unrecognized database-records journal: {path}")
    if journal["home"] != str(home):
        raise RefusedError(f"{path} belongs to {journal['home']}")
    if journal["runs"] and (ran := journal["runs"][-1]["adoption"]) != (now := adoption(home)):
        raise RefusedError(
            f"{path} records a run of adoption {ran}, not this home's {now}: a rollback (R2) "
            "restored the database it repaired. Move cutover-rollback/db-records aside"
        )
    return journal


def _write_journal(home: Path, journal: dict[str, Any]) -> None:
    body = json.dumps(journal, indent=2, sort_keys=True, default=str) + "\n"
    write_private_bytes(home / JOURNAL, body.encode())


def _as_json(document: Any) -> Any:
    """The value as the journal will read it back (UUIDs and times as strings)."""
    return json.loads(json.dumps(document, default=str))


def _store_attestations(home: Path, inputs: Inputs) -> None:
    """The cutover record keeps every attestation verbatim, named by its sha256."""
    directory = ensure_private_dir(home / RECORD / "attestations")
    for machine, raw in inputs.raw.items():
        path = directory / f"{inputs.digest(machine)}.json"
        if not path.exists():
            write_private_bytes(path, raw)
        elif path.read_bytes() != raw:
            raise RuntimeError(f"{path} holds other bytes than its digest names")


def _begin(
    conn: psycopg.Connection[Any], home: Path, inputs: Inputs, own: tuple[str, str], *, later: bool
) -> dict[str, Any]:
    """Plan a new run and record it, every effect with its before image, before any write.

    A run with no effect is recorded too: its fenced summary is the cutover
    record of what stays fenced.
    """
    with conn.transaction():
        found = survey(conn, inputs)
    plan = plan_repairs(found, inputs, own, later=later)
    if plan.refusals:
        raise RefusedError("; ".join(plan.refusals))
    fenced = fenced_summary(found.classified)
    _print_fenced(fenced)
    _store_attestations(home, inputs)
    return _as_json(
        {
            "started_at": datetime.now(UTC).isoformat(),
            "inputs": inputs.record(),
            "adoption": adoption(home),
            # Survey-derived, so not part of the inputs a continuation must match.
            "fenced": fenced,
            "state": "started",
            "effects": plan.effects,
            "results": {step: [] for step in STEPS},
        }
    )


def _ceiling(effect: dict[str, Any], exc: psycopg.Error) -> RuntimeError:
    """The effect hit a session ceiling; its transaction already rolled back."""
    if isinstance(exc, psycopg.errors.LockNotAvailable):
        cause = (
            f"waited longer than lock_timeout {LOCK_TIMEOUT} for a row it locks; another "
            "session or a prepared transaction (D-11) holds it"
        )
    else:
        cause = f"ran longer than statement_timeout {STATEMENT_TIMEOUT}, or was cancelled"
    return RuntimeError(
        f"{effect['op']}: {cause}. Nothing of this effect committed; resolve the cause, "
        "then re-run with the same inputs to continue"
    )


def _print_fenced(fenced: list[dict[str, Any]]) -> None:
    """What this run leaves fenced, stated before its first write."""
    total = sum(group["count"] for group in fenced)
    if not total:
        print("  no retired-shape or identity-less row stays fenced.")
        return
    print(
        f"  {total} agent(s) stay fenced after this run; the runtime keeps refusing them "
        '(conventions/cutover-db-records.md, "Rows left fenced"):'
    )
    for group in fenced:
        more = " ..." if group["count"] > len(group["agents"]) else ""
        agents = ", ".join(map(str, group["agents"])) + more
        print(f"  - {group['count']} {group['verdict']}: {group['reason']} (agents {agents})")


def execute(
    conn: psycopg.Connection[Any], home: Path, inputs: Inputs, own: tuple[str, str]
) -> dict[str, Any]:
    """Continue an incomplete run with the same inputs, or begin a new one; apply its
    effects. The same inputs as a completed run change nothing and return that run."""
    journal = read_journal(home) or {"version": VERSION, "home": str(home), "runs": []}
    runs: list[dict[str, Any]] = journal["runs"]
    same = bool(runs) and runs[-1]["inputs"] == _as_json(inputs.record())
    if runs and runs[-1]["state"] != "done":
        if not same:
            raise RefusedError(
                "an incomplete run recorded other inputs; re-run with the same inputs"
            )
        _print_fenced(runs[-1]["fenced"])
    elif same:
        return runs[-1]
    else:
        runs.append(_begin(conn, home, inputs, own, later=completed(journal)))
        _write_journal(home, journal)
    run = runs[-1]
    for step in STEPS:
        for effect in run["effects"][step][len(run["results"][step]) :]:
            try:
                with conn.transaction():
                    result = _HANDLERS[effect["op"]](conn, effect, inputs)
            except (psycopg.errors.LockNotAvailable, psycopg.errors.QueryCanceled) as exc:
                raise _ceiling(effect, exc) from exc
            run["results"][step].append(result)
            _write_journal(home, journal)
    run["state"] = "done"
    _write_journal(home, journal)
    return run


def owner_authority(home: Path, registry: Path) -> OwnerAuthority:
    """The home's administrator acting as its schema owner, over its owner-only socket."""
    from dotenv import dotenv_values
    from psycopg.conninfo import conninfo_to_dict

    from shared.cluster import load_registry, record_postgres_port
    from shared.pg_admin import pg_socket_path

    record = load_registry(path=registry).get(str(home))
    if record is None:
        raise RefusedError(f"{home} has no registry record in {registry}; run on the gateway home")
    url = dotenv_values(home / ".env").get("AVA_DB_URL")
    parts = conninfo_to_dict(url) if url else {}
    owner, database = parts.get("user"), parts.get("dbname")
    if not (isinstance(owner, str) and owner and isinstance(database, str) and database):
        raise RefusedError(f"{home / '.env'} AVA_DB_URL names no schema owner and database")
    socket, port = pg_socket_path(home), record_postgres_port(record)
    admin = f"postgresql://{getpass.getuser()}@/postgres?host={socket}&port={port}"
    return OwnerAuthority(admin_url=admin, database=database, owner=owner, data_dir=home / "pg")


@contextmanager
def session(home: Path, registry: Path, *, write: bool) -> Generator[psycopg.Connection[Any]]:
    """The owner session. Read modes also run against the legacy postmaster, which
    has no custody record; `--execute` binds the session to the home's postmaster."""
    from shared.pg_admin import owner_session

    authority = owner_authority(home, registry)
    with owner_session(
        authority.admin_url,
        database=authority.database,
        owner=authority.owner,
        expected_data_dir=authority.data_dir if write else None,
        autocommit=True,
    ) as conn:
        prepare_session(conn, write=write)
        yield conn


def prepare_session(conn: psycopg.Connection[Any], *, write: bool) -> None:
    """UTC renders every recorded timestamp identically across runs; reads see one
    snapshot; no statement waits on a lock or runs past the session ceilings."""
    conn.execute("SET TIME ZONE 'UTC'")
    conn.execute(
        "SELECT set_config('lock_timeout', %s, false), set_config('statement_timeout', %s, false)",
        (LOCK_TIMEOUT, STATEMENT_TIMEOUT),
    )
    if not write:
        conn.execute(
            "SET SESSION CHARACTERISTICS AS TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"
        )


def _json_object(path: str | None, flag: str) -> dict[str, Any] | None:
    if path is None:
        return None
    try:
        document: object = json.loads(Path(path).read_text())
    except (OSError, ValueError) as exc:
        raise RefusedError(f"{flag} {path}: {exc}") from exc
    if not isinstance(document, dict):
        raise RefusedError(f"{flag} {path} must hold one JSON object")
    return cast("dict[str, Any]", document)


def read_inputs(args: argparse.Namespace) -> Inputs:
    if args.execute and not (args.operator and args.reason):
        raise RefusedError("--execute requires --operator and --reason (both are recorded)")
    attestations: dict[str, Attestation] = {}
    raw: dict[str, bytes] = {}
    for path in args.attestation:
        doc, data = load_attestation(Path(path))
        if doc.machine in attestations:
            raise RefusedError(f"two attestations for machine {doc.machine}")
        attestations[doc.machine], raw[doc.machine] = doc, data
    try:
        units = (
            _UNITS.validate_json(Path(args.retire_units).read_bytes()) if args.retire_units else ()
        )
    except (OSError, ValidationError) as exc:
        raise RefusedError(f"--retire-units: {exc}") from exc
    return Inputs(
        operator=args.operator,
        reason=args.reason,
        pending=_json_object(args.pending_json, "--pending-json"),
        lease=_json_object(args.lease_json, "--lease-json"),
        retire_units=units,
        attestations=attestations,
        raw=raw,
        legacy_commit=resolve_commit(args.legacy_commit) if args.legacy_commit else None,
    )


def resolve_commit(ref: str) -> str:
    result = subprocess.run(  # noqa: S603 — fixed git argv, operator-named ref
        ["git", "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"],
        cwd=own_checkout(),
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise RefusedError(f"--legacy-commit {ref} is not a commit in {own_checkout()}")
    return result.stdout.strip()


def own_unit(home: Path) -> tuple[str, str]:
    """This gateway's own `machine_units` key; it is never retired."""
    from cli.start_intent import _stored

    return _stored(home)["AVA_MACHINE_NAME"], str(home)


def _print_plan(found: Survey, plan: Plan) -> None:
    for step in STEPS:
        print(f"  {step}: {len(plan.effects[step])} effect(s)")
        for effect in plan.effects[step]:
            shown = {k: v for k, v in effect.items() if k not in {"op", "before", "after", "rows"}}
            print(f"    - {effect['op']} {json.dumps(shown, sort_keys=True, default=str)}")
    listed: dict[tuple[str, str], list[int]] = {}
    for item in found.classified:
        if item.verdict != "convertible":
            listed.setdefault((item.verdict, str(item.reason)), []).append(item.agent_id)
    for (verdict, reason), agents in sorted(listed.items()):
        shown = ", ".join(map(str, agents[:20])) + (" ..." if len(agents) > 20 else "")
        print(f"  - {len(agents)} row(s) stay {verdict} ({reason}): agents {shown}")
    for reason in plan.refusals:
        print(f"  ✗ refused: {reason}")


def _check(conn: psycopg.Connection[Any], home: Path, inputs: Inputs, rows_out: str | None) -> int:
    with conn.transaction():
        found = survey(conn, inputs)
    plan = plan_repairs(found, inputs, own_unit(home), later=completed(read_journal(home)))
    if rows_out:
        Path(rows_out).write_text(json.dumps(export_rows(found), indent=2) + "\n")
    report = {
        "version": VERSION,
        "home": str(home),
        "checks": found.checks,
        "refusals": plan.refusals,
    }
    print(json.dumps(report, indent=2, sort_keys=True, default=str))
    clean = all(check["verdict"] in {"ok", "info"} for check in found.checks.values())
    return 0 if clean and not plan.refusals else 2


def _run(conn: psycopg.Connection[Any], home: Path, inputs: Inputs) -> int:
    from shared.platform import file_lock

    ensure_private_dir(home / ARCHIVE)
    ensure_private_dir(home / RECORD)
    with file_lock(home / RECORD / "journal.lock", timeout_s=30):
        run = execute(conn, home, inputs, own_unit(home))
    if not any(run["effects"].values()):
        print(f"✓ nothing to repair; the run is recorded in {home / JOURNAL}.")
        return 0
    noted = False  # a result with a note: a refused conversion, rows a mint left unchanged
    for step in STEPS:
        results = [str(result) for result in run["results"][step]]
        outcome = [result.split(":", 1)[0] for result in results]
        counts = ", ".join(f"{name} {outcome.count(name)}" for name in sorted(set(outcome)))
        print(f"  {step}: {counts or 'none'}")
        for result in (result for result in results if ":" in result):
            noted = True
            print(f"    ! {result}")
    done = "! repairs recorded with the exceptions above" if noted else "✓ repairs recorded"
    print(f"{done} in {home / JOURNAL}; run --check to verify.")
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument("--home", required=True, help="the gateway home (explicit)")
    parser.add_argument("--registry", help="cluster registry (default: the home's, else ~/.ava)")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true", help="read-only D-series inventory (JSON)")
    mode.add_argument("--execute", action="store_true", help="apply the repairs")
    parser.add_argument("--rows-out", help="--check: write the legacy identities for --attest")
    parser.add_argument("--legacy-commit", help="the commit every host ran (D-3, D-4)")
    parser.add_argument("--attestation", action="append", default=[], help="one per machine")
    parser.add_argument("--pending-json", help="the exact pending publication to clear")
    parser.add_argument("--lease-json", help="the exact deploy lease columns to release")
    parser.add_argument("--retire-units", help="JSON list of {machine, home, evidence}")
    parser.add_argument("--operator", help="who performs the repair (recorded)")
    parser.add_argument("--reason", help="why (recorded)")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        home = canonical_home(args.home)
        inputs = read_inputs(args)
        with session(home, registry_path(home, args.registry), write=args.execute) as conn:
            if args.check:
                return _check(conn, home, inputs, args.rows_out)
            if args.execute:
                return _run(conn, home, inputs)
            with conn.transaction():
                found = survey(conn, inputs)
            plan = plan_repairs(found, inputs, own_unit(home), later=completed(read_journal(home)))
    except RefusedError as exc:
        print(f"✗ refused, nothing changed by this run: {exc}", file=sys.stderr)
        return 1
    except (RuntimeError, ValueError, OSError, psycopg.Error) as exc:
        print(f"✗ incomplete; fix the cause and re-run to continue: {exc}", file=sys.stderr)
        return 1
    _print_plan(found, plan)
    print("[dry-run] no changes made.")
    return 2 if plan.refusals else 0


if __name__ == "__main__":
    raise SystemExit(main())
