#!/usr/bin/env python3
"""One-time adoption of a legacy-born home into the new lifecycle (production cutover).

A home born by legacy code has no `start-intent.json`, no `AVA_SERVICE_PATH`,
legacy OS jobs, legacy state files and, on a former gateway now serving as a
remote agent-runner, a gateway-shaped registry record plus gateway-only
material. Ordinary `ava start` never adopts such a home implicitly; this script
is the one explicit conversion (the fleet cutover plan, steps W5 and W9). It is
deleted after the cutover with the other `cutover_*` scripts.

Dry-run is the default and changes nothing: it prints the plan and every
refusal, computed by `scripts/cutover_adopt_plan.py` from the same facts
`scripts/cutover_inventory.py` reports. `--execute` refuses before any effect
unless the home is stopped (no live home process, no bound data-plane port)
and unambiguous, then runs the steps in order. Nothing is deleted: legacy
files, residue, plists, unit files and the crontab pre-image move under
`$AVA_HOME/cutover-rollback/`. Identity is adopted, never re-minted: the
intent carries the existing registry record (gateway) or none (remote unit),
the persisted machine identity and the operator-supplied `AVA_SERVICE_PATH`.

Journal: `$AVA_HOME/cutover-rollback/adopt-home.json` (0600). The first
execute records the inputs and mints the cutover hold identity before any
effect. Each step records `started` with its planned effects before acting
and `done` with the effects it applied. A re-run continues from the journal
(every effect is idempotent); a completed journal re-verifies and changes
nothing. A re-run must name the same inputs.

`--start` performs the held first start after adoption (and, on a gateway,
after the data-plane authority cutover and the database-records repair, which
it checks): the ordinary `ava start` inside the cutover hold's authorized-start
boundary, moving the hold `stopped -> starting -> ready`. Business stays
closed until the operator releases the hold at the go/no-go gate with
`--resume`, its one exit: it refuses unless the adoption completed, the hold is
`ready` and, on a gateway, the database-records repair recorded a completed
run, then resumes exactly that hold. No ordinary path releases the cutover
hold (`cli.cutover_hold`): before this held first start an ordinary start
refuses, afterwards (a reboot's autostart) it starts held, and
`ava cluster recover` and `ava maintenance resume` refuse it.

Run from the checkout that owns the home:

    .venv/bin/python scripts/cutover_adopt_home.py --home ~/.ava --service-path P
    .venv/bin/python scripts/cutover_adopt_home.py --home ~/.ava --service-path P \
        --execute --expect-mode gateway|remote-unit
    .venv/bin/python scripts/cutover_adopt_home.py --home ~/.ava --start
    .venv/bin/python scripts/cutover_adopt_home.py --home ~/.ava --resume

`--execute` names the mode the operator expects (`--expect-mode`); the mode
the capability files imply must match, since a remote unit's adoption strips
its credentials and moves `pg/`, `backups/` and `secrets/`. A home with its
own `source` checkout adopts and starts only from it: a throwaway checkout
whose `.ava_home` names the home serves the dry-run only.

A remote unit's `.env` loses the human bearer (`AVA_CLUSTER_SECRET`) with the
other gateway-only keys; its held start installs the unit capability bundle the
gateway's data-plane cutover issued (`--start --db-capability BUNDLE`, transport
key in `AVA_DB_CAPABILITY_KEY`).
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable
from dataclasses import asdict, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from cli.cutover_hold import (
    ADOPTION_JOURNAL,
    CUTOVER_HOLDER_PREFIX,
    held_start_command,
    release_command,
)
from scripts.cutover_adopt_plan import (
    SELECTION,
    STEPS,
    archive_destination,
    intent_document,
    plan,
    refusals,
    selection_payload,
)
from scripts.cutover_inventory import (
    ARCHIVE,
    PAUSE_OWNER,
    Facts,
    Inputs,
    RefusedError,
    canonical_home,
    dead_keys_are_dead,
    gather,
    own_checkout,
    read_journal,
    registry_path,
    verdict,
)
from scripts.cutover_legacy_jobs import (
    Host,
    LaunchdJob,
    SystemdUnit,
    remove_cron_lines,
    retire_launchd,
    retire_unit,
)
from shared import cluster
from shared.private_storage import ensure_private_dir, write_private_bytes

_AUDIT = "cutover_adopt_home"


def require_owning_checkout(home: Path, checkout: Path, *, effects: bool) -> None:
    """The checkout the intent records must be the one the home's services run.

    A throwaway checkout whose `.ava_home` names the home may plan (the T-3
    dry-run); a home with its own `source` checkout (production) adopts and
    starts only from it, so no intent is bound to a disposable path.
    """
    pointer = checkout / ".ava_home"
    bound = pointer.is_file() and pointer.read_text().strip() == str(home)
    source = home / "source"
    if checkout != source and not bound:
        raise RefusedError(
            f"run this script from the checkout that owns {home} ({source} or a "
            f"checkout whose .ava_home names it), not {checkout}"
        )
    if effects and source.is_dir() and checkout != source:
        raise RefusedError(
            f"{home} runs from {source}: --execute and --start run only from it; "
            f"{checkout} may only plan the adoption"
        )


def mode_refusal(facts: Facts, expected: str) -> str | None:
    """The mode is inferred from capability files; the operator states it too."""
    mode = "gateway" if facts.gateway else "remote-unit"
    if mode == expected:
        return None
    return (
        f"--expect-mode {expected}, but {facts.home} reads as a {mode} "
        f"(roles {', '.join(facts.roles) or 'none'}); check its machine_serve_* files"
    )


def _write_journal(home: Path, journal: dict[str, Any]) -> None:
    ensure_private_dir((home / ADOPTION_JOURNAL).parent)
    write_private_bytes(
        home / ADOPTION_JOURNAL, (json.dumps(journal, indent=2, sort_keys=True) + "\n").encode()
    )


def _new_journal(facts: Facts, inputs: Inputs, cutover_id: str) -> dict[str, Any]:
    """Inputs and the cutover hold identity, recorded before the first effect."""
    now = datetime.now(UTC).replace(microsecond=0)
    owner = facts.pause_owner
    hold = (
        {"holder": owner["holder"], "acquired_at": owner["acquired_at"], "origin": "legacy-stop"}
        if owner["adoptable"]
        else {
            "holder": f"{CUTOVER_HOLDER_PREFIX}{cutover_id}",
            "acquired_at": now.isoformat(),
            "origin": "cutover",
        }
    )
    home, registry, checkout = facts.home, facts.registry, facts.checkout
    return {
        "version": 1,
        "home": str(home),
        "cutover_id": cutover_id,
        "created_at": now.isoformat(),
        "inputs": {
            "service_path": inputs.service_path,
            "keep_secrets": list(inputs.keep_secrets),
            "registry": str(registry),
            "checkout": str(checkout),
        },
        "hold": hold,
        "steps": {},
    }


def reconcile_inputs(
    journal: dict[str, Any] | None, inputs: Inputs, registry: Path, checkout: Path
) -> Inputs:
    """A continuation runs with the recorded inputs; a different explicit one refuses."""
    if journal is None:
        return inputs
    recorded = journal["inputs"]
    if inputs.service_path is not None and inputs.service_path != recorded["service_path"]:
        raise RefusedError("--service-path differs from the value this adoption recorded")
    if inputs.keep_secrets and list(inputs.keep_secrets) != recorded["keep_secrets"]:
        raise RefusedError("--keep-secret differs from this adoption's record")
    if (recorded["registry"], recorded["checkout"]) != (str(registry), str(checkout)):
        raise RefusedError("the registry or checkout differs from this adoption's record")
    return Inputs(recorded["service_path"], tuple(recorded["keep_secrets"]))


def _archive(home: Path, effect: dict[str, Any]) -> None:
    source = home / effect["src"]
    destination = home / archive_destination(effect)
    if not (source.exists() or source.is_symlink()):
        return
    if destination.exists() or destination.is_symlink():
        raise RuntimeError(f"both {source} and its archive copy exist; resolve by hand")
    ensure_private_dir(destination.parent)
    source.rename(destination)


def _create_hold(home: Path, journal: dict[str, Any]) -> None:
    """The cutover hold: the existing pause-owner journal, maintenance phase `stopped`."""
    from shared import pause_owner
    from shared.maintenance_state import MaintenanceHold
    from shared.platform import file_lock

    holder, at = journal["hold"]["holder"], journal["hold"]["acquired_at"]
    path = home / PAUSE_OWNER
    ensure_private_dir(path.parent)
    with file_lock(path.parent / "deploy-pause-owner.lock", timeout_s=pause_owner._LOCK_TIMEOUT_S):
        current = pause_owner.read_for_home(home)
        if current.status != "inactive":
            if current.status == "paused" and current.holder == holder:
                return
            raise RuntimeError(f"{PAUSE_OWNER} changed under the adoption ({current.status})")
        pause_owner._write_atomic(
            path,
            {
                "state": "paused",
                "holder": holder,
                "acquired_at": at,
                "maintenance": MaintenanceHold(phase="stopped").encode(),
                "driver": None,
            },
        )
    written = pause_owner.read_for_home(home)
    if written.maintenance is None or written.maintenance.phase != "stopped":
        raise RuntimeError("the cutover hold did not read back as a stopped maintenance hold")


def _require_hold(home: Path, journal: dict[str, Any], phase: str) -> None:
    """The adopted legacy hold is still exactly the one the journal recorded."""
    from shared import pause_owner

    holder, at = journal["hold"]["holder"], datetime.fromisoformat(journal["hold"]["acquired_at"])
    current = pause_owner.read_for_home(home)
    if not (current.status == "paused" and current.matches(holder, at) and current.maintenance):
        raise RuntimeError(f"the adopted hold {holder} is no longer standing")
    if current.maintenance.phase != phase:
        raise RuntimeError(f"the adopted hold {holder} moved to {current.maintenance.phase}")


def _record_complete(registry: Path, home: Path, ports: dict[str, int]) -> None:
    with cluster.registry_lock(path=registry):
        records = cluster.load_registry(path=registry)
        record = records[str(home)]
        current = dict(cast("dict[str, int]", record.ports))
        for key, port in ports.items():
            if key in current and current[key] != port:
                raise RuntimeError(
                    f"registry port {key} changed to {current[key]} under the adoption"
                )
        merged = {**current, **ports}
        if merged != current:
            updated = replace(record, ports=cast("cluster.ClusterPorts", merged))
            cluster.save_record_locked(updated, path=registry)


def _record_retire(registry: Path, home: Path, before: dict[str, Any]) -> None:
    from shared.cluster.registry import _dump_registry

    with cluster.registry_lock(path=registry):
        records = cluster.load_registry(path=registry)
        record = records.get(str(home))
        if record is None:
            return
        if asdict(record) != before:
            raise RuntimeError("the registry record changed under the adoption; resolve by hand")
        del records[str(home)]
        _dump_registry(records, path=registry)


def _write_intent(facts: Facts, inputs: Inputs) -> None:
    from cli.start_identity import INTENT_NAME, _write, read_intent

    record = cluster.load_registry(path=facts.registry).get(str(facts.home))
    service_path = inputs.service_path or facts.env.get("AVA_SERVICE_PATH")
    if not service_path:
        raise RuntimeError("no AVA_SERVICE_PATH to record in the start intent")
    document = intent_document(facts, service_path, asdict(record) if record else None)
    existing = read_intent(facts.home)
    if existing is not None:
        if existing != document:
            raise RuntimeError("a start intent exists and differs from the adopted identity")
        return
    _write(facts.home / INTENT_NAME, document)
    if read_intent(facts.home) != document:
        raise RuntimeError("the adopted start intent did not read back")


Handler = Callable[[dict[str, Any]], object]


def _handlers(
    facts: Facts, inputs: Inputs, host: Host, journal: dict[str, Any]
) -> dict[str, Handler]:
    """One idempotent handler per effect kind the planner emits."""
    from shared.envfile import remove_env, upsert_env

    home = facts.home
    jobs = home / ARCHIVE / "os-jobs"
    return {
        "launchd-retire": lambda effect: retire_launchd(
            host,
            LaunchdJob(
                kind=effect["kind"],
                label=effect["label"],
                plist=effect["plist"],
                present=True,
                loaded=True,
                pid=None,
                retire=True,
                reused=False,
            ),
            jobs,
        ),
        "unit-retire": lambda effect: retire_unit(
            host,
            SystemdUnit(
                kind=effect["kind"],
                unit=effect["unit"],
                path=effect["path"],
                scope=effect["scope"],
                reused=False,
            ),
            jobs,
        ),
        # Crontab lines are removed together, under one lock, before the loop.
        "cron-remove": lambda _effect: None,
        "archive": lambda effect: _archive(home, effect),
        "hold-create": lambda _effect: _create_hold(home, journal),
        "hold-adopt": lambda _effect: _require_hold(home, journal, "stopped"),
        "selection-write": lambda effect: write_private_bytes(
            home / SELECTION, selection_payload(effect["names"])
        ),
        "env-set": lambda effect: upsert_env(
            home / ".env", {effect["key"]: effect["value"]}, audit_site=_AUDIT
        ),
        "env-remove": lambda effect: remove_env(
            home / ".env", set(effect["keys"]), audit_site=_AUDIT
        ),
        "record-complete": lambda effect: _record_complete(facts.registry, home, effect["ports"]),
        "record-retire": lambda effect: _record_retire(facts.registry, home, effect["record"]),
        "intent-write": lambda _effect: _write_intent(facts, inputs),
    }


def _apply(
    effects: list[dict[str, Any]], facts: Facts, inputs: Inputs, host: Host, journal: dict[str, Any]
) -> None:
    # Crontab first: on Linux it carries the auto-rollback health probe and the
    # watchdogs, the actors that must be disarmed before anything else.
    cron = tuple(effect["line"] for effect in effects if effect["op"] == "cron-remove")
    if cron:
        remove_cron_lines(host, cron, facts.home / ARCHIVE / "os-jobs")
    handlers = _handlers(facts, inputs, host, journal)
    for effect in effects:
        handlers[effect["op"]](effect)


def _run_steps(
    journal: dict[str, Any], home: Path, registry: Path, checkout: Path, host: Host, inputs: Inputs
) -> None:
    for name in STEPS:
        recorded = journal["steps"].get(name)
        if recorded is not None and recorded["state"] == "done":
            continue
        # Fresh facts per step: an earlier step's effects (a `.env` write
        # snapshotting into backups/) can change what a later step must move.
        facts = gather(home, registry, checkout, host, inputs, live=False)
        facts.journal = journal
        effects = next(step.effects for step in plan(facts, inputs) if step.name == name)
        history = list(recorded["effects"]) if recorded else []
        history += [effect for effect in effects if effect not in history]
        journal["steps"][name] = {"state": "started", "effects": history}
        _write_journal(home, journal)
        _apply(list(effects), facts, inputs, host, journal)
        journal["steps"][name] = {"state": "done", "effects": history}
        _write_journal(home, journal)
        print(f"  {name}: {len(effects)} effect(s) applied")


def verify(home: Path, registry: Path, checkout: Path, journal: dict[str, Any]) -> None:
    """The next `ava start` identity phase must accept the adopted home as-is."""
    from dotenv import dotenv_values

    from cli.start_identity import IdentityInput, prepare_identity, read_intent
    from cli.start_intent import _stored

    intent = read_intent(home)
    if intent is None or intent["phase"] not in {"provisioned", "ready"}:
        raise RuntimeError("the adopted home has no provisioned start intent")
    record = cluster.load_registry(path=registry).get(str(home))
    if intent["record"] != (asdict(record) if record else None):
        raise RuntimeError("the start intent and the registry record disagree")
    values = _stored(home)
    values["AVA_SERVICE_PATH"] = str(dotenv_values(home / ".env")["AVA_SERVICE_PATH"])
    prepare_identity(
        IdentityInput(
            home=home,
            registry=registry,
            checkout=checkout,
            worktree=False,
            roles=frozenset(intent["roles"]),
            values=values,
        )
    )
    if not all(journal["steps"][name]["state"] == "done" for name in STEPS):
        raise RuntimeError("the adoption journal is incomplete")


def _complete(journal: dict[str, Any] | None) -> bool:
    return journal is not None and all(
        name in journal["steps"] and journal["steps"][name]["state"] == "done" for name in STEPS
    )


def execute(
    home: Path,
    registry: Path,
    checkout: Path,
    host: Host,
    inputs: Inputs,
    cutover_id: str,
    expect_mode: str,
) -> dict[str, Any]:
    """Adopt `home`; refusals raise before this run changes anything."""
    journal = read_journal(home)
    if _complete(journal):
        assert journal is not None  # noqa: S101 — _complete checked
        verify(home, registry, checkout, journal)
        return journal
    inputs = reconcile_inputs(journal, inputs, registry, checkout)
    dead_keys_are_dead()
    facts = gather(home, registry, checkout, host, inputs)
    facts.journal = journal
    reasons = refusals(facts, inputs)
    if (mismatch := mode_refusal(facts, expect_mode)) is not None:
        reasons.append(mismatch)
    if reasons:
        raise RefusedError("; ".join(reasons))
    if journal is None:
        journal = _new_journal(facts, inputs, cutover_id)
        _write_journal(home, journal)
    _run_steps(journal, home, registry, checkout, host, inputs)
    verify(home, registry, checkout, journal)
    return journal


def held_start(home: Path, db_capability: str | None = None) -> int:
    """The first start with new code, inside the cutover hold; the hold stays closed.

    A remote unit's first start installs its capability bundle (`db_capability`,
    transport key in AVA_DB_CAPABILITY_KEY): it holds no human bearer any more.
    A gateway's start runs only after the database-records repair (W7) recorded a
    completed run: the start writes the host's `paused` posture, which the
    repair's first run would set `idle`. A runner's start joins through that
    gateway's bootstrap, so it cannot precede W7 either.
    """
    from shared import pause_owner

    journal = read_journal(home)
    if not _complete(journal):
        raise RefusedError("adoption is not complete; run --execute first")
    assert journal is not None  # noqa: S101 — _complete checked
    holder, raw = journal["hold"]["holder"], journal["hold"]["acquired_at"]
    at = datetime.fromisoformat(raw)
    current = pause_owner.read_for_home(home)
    if current.status != "paused" or not current.matches(holder, at) or current.maintenance is None:
        raise RefusedError(f"the cutover hold {holder} is not standing on this home")
    phase = current.maintenance.phase
    if phase not in {"stopped", "starting", "ready"}:
        raise RefusedError(f"the cutover hold is in phase {phase}, not a held start phase")
    if phase != "ready":
        if "gateway" in _adopted_roles(home) and (missing := _records_repair_missing(home)):
            raise RefusedError(
                f"{missing}. A gateway's held first start (W8) follows that repair: run "
                "scripts/cutover_db_records.py --execute to completion first; this start "
                "writes the host's `paused` posture, which the repair's first run sets idle"
            )
        rc = _start_inside_hold(home, holder, at, phase, db_capability)
        if rc != 0:
            return rc
    print(f"✓ {home} is started and held. At the go/no-go gate release it with:")
    print(f"  {release_command(home)}")
    return 0


def release(home: Path) -> int:
    """The go/no-go gate: release the cutover hold, its one exit, which opens business.

    Refuses unless the adoption completed, the held first start reached `ready`
    and, on a gateway, the database-records repair (W7) recorded a completed
    run. `ava maintenance resume` then releases the hold as for any other: the
    unit must be serving, and the agents the hold drained are woken. The rest
    of the gate (the alert path, an empty legacy census on every host, the
    stale-writer probe) is the operator's to verify first. No agent runs under
    the hold, so the smoke agent follows each release, gateway first, before the
    next unit is released (conventions/cutover-home-adoption.md).
    """
    from cli.commands.maintenance import resume
    from shared import pause_owner
    from shared.paths import ava_home
    from shared.release_operation import require_start_authorized

    journal = read_journal(home)
    if not _complete(journal):
        raise RefusedError("adoption is not complete; run --execute first")
    assert journal is not None  # noqa: S101 — _complete checked
    holder, at = journal["hold"]["holder"], datetime.fromisoformat(journal["hold"]["acquired_at"])
    current = pause_owner.read_for_home(home)
    if current.status == "resumed" and current.matches(holder, at):
        print(f"✓ the cutover hold {holder} is already released on {home}.")
        return 0
    if current.status != "paused" or not current.matches(holder, at) or current.maintenance is None:
        raise RefusedError(f"the cutover hold {holder} is not standing on this home")
    if current.maintenance.phase != "ready":
        raise RefusedError(
            f"the cutover hold is in phase {current.maintenance.phase}; its held first start "
            f"{held_start_command(home)} has not passed readiness"
        )
    if ava_home().resolve() != home:
        raise RefusedError(f"this checkout's home is {ava_home()}, not {home}")
    if "gateway" in _adopted_roles(home) and (missing := _records_repair_missing(home)):
        raise RefusedError(missing)
    require_start_authorized(home)
    resume(holder, at, cancel=False)
    print(f"✓ cutover hold {holder} released: business is open on {home}.")
    return 0


def _adopted_roles(home: Path) -> frozenset[str]:
    from cli.start_identity import read_intent

    intent = read_intent(home)
    if intent is None:
        raise RefusedError(f"{home} has no start intent")
    return frozenset(intent["roles"])


def _records_repair_missing(home: Path) -> str | None:
    """Why the gateway's database-records repair (W7) is not on record as complete."""
    from scripts.cutover_db_records import JOURNAL
    from scripts.cutover_db_records import read_journal as read_records

    runs = (read_records(home) or {"runs": []})["runs"]
    if not runs:
        return f"the database-records repair (W7) recorded no run in {home / JOURNAL}"
    if runs[-1]["state"] != "done":
        return "the last database-records repair run is incomplete; continue it with its inputs"
    return None


def _start_inside_hold(
    home: Path, holder: str, at: datetime, phase: str, db_capability: str | None
) -> int:
    from shared import maintenance, start_serving
    from shared.exit_codes import SERVICES_NOT_READY_EXIT_CODE
    from shared.paths import ava_home

    if ava_home().resolve() != home:
        raise RefusedError(f"this checkout's home is {ava_home()}, not {home}")
    if phase == "stopped":
        maintenance.set_phase(holder, at, "starting")
    from cli.parsers import build_parser
    from cli.start_intent import run_start

    argv = ["start"] if db_capability is None else ["start", "--db-capability", db_capability]
    args = build_parser().parse_args(argv)
    with maintenance.authorized_start(holder, at):
        rc = run_start(args)
    if rc != 0:
        return rc
    if not start_serving.is_serving():
        return SERVICES_NOT_READY_EXIT_CODE
    maintenance.set_phase(holder, at, "ready")
    return 0


def _print_plan(report: dict[str, Any]) -> None:
    print(f"home {report['home']} ({report['mode']}, roles {','.join(report['roles']) or '-'})")
    for step in report["plan"]:
        print(f"  {step['step']}: {len(step['effects'])} effect(s)")
        for effect in step["effects"]:
            detail = {key: value for key, value in effect.items() if key not in {"op", "record"}}
            print(f"    - {effect['op']} {json.dumps(detail, sort_keys=True)}")
    for reason in report["refusals"]:
        print(f"  ✗ refused: {reason}")


def _arguments(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument("--home", required=True, help="the home to adopt (explicit)")
    parser.add_argument("--service-path", help="the reviewed AVA_SERVICE_PATH to record")
    parser.add_argument("--registry", help="cluster registry (default: the home's, else ~/.ava)")
    parser.add_argument(
        "--db-capability",
        help="--start on a remote unit: its capability bundle (key in AVA_DB_CAPABILITY_KEY)",
    )
    parser.add_argument("--keep-secret", action="append", default=[], help="secrets/ entry to keep")
    parser.add_argument("--cutover-id", help="names the cutover hold (default: minted)")
    parser.add_argument("--json", action="store_true", help="dry-run: print the full JSON verdict")
    parser.add_argument(
        "--expect-mode",
        choices=("gateway", "remote-unit"),
        help="required with --execute: the mode the operator expects this home to adopt as",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--execute", action="store_true", help="perform the adoption")
    mode.add_argument("--start", action="store_true", help="held first start after adoption")
    mode.add_argument(
        "--resume", action="store_true", help="go/no-go gate: release the cutover hold"
    )
    args = parser.parse_args(argv)
    if args.db_capability is not None and not args.start:
        parser.error("--db-capability belongs to --start")
    if args.execute and args.expect_mode is None:
        parser.error("--execute requires --expect-mode gateway|remote-unit")
    args.effects = args.execute or args.start or args.resume
    return args


def main(
    argv: list[str] | None = None, *, host: Host | None = None, checkout: Path | None = None
) -> int:
    args = _arguments(argv)
    try:
        home = canonical_home(args.home)
        owner = checkout or own_checkout()
        require_owning_checkout(home, owner, effects=args.effects)
        if args.start:
            return held_start(home, args.db_capability)
        if args.resume:
            return release(home)
        inputs = Inputs(args.service_path, tuple(args.keep_secret))
        registry = registry_path(home, args.registry)
        host = host or Host.current()
        if args.execute:
            from shared.platform import file_lock

            cutover_id = args.cutover_id or datetime.now(UTC).strftime("adopt-%Y%m%dT%H%M%SZ")
            with file_lock(home / "start-intent.lock", timeout_s=30):
                journal = execute(home, registry, owner, host, inputs, cutover_id, args.expect_mode)
            hold = journal["hold"]
            print(f"✓ adoption complete; cutover hold {hold['holder']} @ {hold['acquired_at']}.")
            print(f"  next: this script --home {home} --start (after any data-plane cutover)")
            return 0
        journal = read_journal(home)
        if _complete(journal):
            print(f"adoption of {home} is complete (journal done); nothing to plan.")
            return 0
        inputs = reconcile_inputs(journal, inputs, registry, owner)
        facts = gather(home, registry, owner, host, inputs)
        report = verdict(facts, inputs)
        if args.expect_mode and (mismatch := mode_refusal(facts, args.expect_mode)):
            report["refusals"].append(mismatch)
            report["adoptable"] = False
    except RefusedError as exc:
        print(f"✗ adoption refused, nothing changed by this run: {exc}", file=sys.stderr)
        return 1
    except (RuntimeError, ValueError, OSError, KeyError) as exc:
        print(
            f"✗ adoption incomplete; fix the cause and re-run to continue: {exc}", file=sys.stderr
        )
        return 1
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        _print_plan(report)
        print("[dry-run] no changes made.")
    return 0 if report["adoptable"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
