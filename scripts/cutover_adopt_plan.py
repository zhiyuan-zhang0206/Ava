"""Refusals and the step plan of the one-time home adoption; pure, no effects.

`scripts/cutover_inventory.py` reports this plan read-only and
`scripts/cutover_adopt_home.py` executes it step by step under its journal.
Both compute it from the same gathered facts, so the inventory verdict is
exactly what an adoption run would change at that moment.

Steps run in this order; each one's effects are idempotent:

1. `jobs` retires the legacy OS jobs (disarm first).
2. `hold` adopts the maintenance hold a completed legacy `ava stop` left
   (phase `stopped`) as the cutover hold; without one, it archives an inert
   legacy pause-owner journal and creates the cutover hold in phase `stopped`.
3. `files` moves inert legacy files aside.
4. `selection` translates `disabled_services` into `service-selection.json`.
5. `env` records `AVA_SERVICE_PATH`, removes dead keys and, on a remote unit,
   the gateway-only keys and the human bearer `AVA_CLUSTER_SECRET` (a remote
   unit authenticates with its capability's machine API token).
6. `residue` moves former-gateway material aside on a remote unit.
7. `record` completes a gateway record's port block, or retires a remote
   unit's gateway-shaped record.
8. `intent` writes `start-intent.json` in phase `provisioned`.

`env` precedes `residue` because every `.env` write snapshots the previous
file into `backups/env/`; moving `backups/` afterwards takes those snapshots
(which still hold the removed keys) into the archive too.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from scripts.cutover_inventory import (
    DISABLED_SERVICES,
    PAUSE_OWNER,
    SELECTION,
    Facts,
    Inputs,
    env_changes,
)

STEPS = ("jobs", "hold", "files", "selection", "env", "residue", "record", "intent")
IDENTITY_KEYS = (
    "AVA_MACHINE_NAME",
    "AVA_MACHINE_HOST",
    "AVA_MACHINE_DESCRIPTION",
    "AVA_MEMORY_REMOTE",
    "AVA_GATEWAY_URL",
)
CAPABILITY_KEYS = {
    "gateway": "AVA_MACHINE_SERVE_GATEWAY",
    "agent-runner": "AVA_MACHINE_SERVE_AGENT_RUNNER",
    "observability-station": "AVA_MACHINE_SERVE_OBSERVABILITY_STATION",
}


@dataclass(frozen=True)
class Step:
    name: str
    effects: tuple[dict[str, Any], ...]

    def encode(self) -> dict[str, Any]:
        return {"step": self.name, "effects": list(self.effects)}


def archive_destination(effect: dict[str, Any]) -> str:
    """Where an `archive` effect moves its source, relative to the home."""
    return f"cutover-rollback/{effect['area']}/{effect['src']}"


def journal_hold(facts: Facts) -> tuple[str, str] | None:
    if facts.journal is None:
        return None
    return facts.journal["hold"]["holder"], facts.journal["hold"]["acquired_at"]


def step_state(facts: Facts, name: str) -> str | None:
    if facts.journal is None or name not in facts.journal["steps"]:
        return None
    return facts.journal["steps"][name]["state"]


def _ours(facts: Facts) -> bool:
    hold = journal_hold(facts)
    owner = facts.pause_owner
    return (
        hold is not None
        and owner["status"] == "paused"
        and (owner["holder"], owner["acquired_at"]) == hold
    )


def selection_payload(names: list[str]) -> bytes:
    """Byte-identical to what `shared.service_selection` writes for an except-list."""
    import json

    return (json.dumps({"version": 1, "mode": "except", "names": sorted(names)}) + "\n").encode()


def _jobs(facts: Facts) -> tuple[dict[str, Any], ...]:
    assert facts.jobs is not None  # noqa: S101 — gathered before planning
    # Once this home's legacy jobs are retired, reused names are current registrations.
    jobs = facts.jobs.retirable(reused=step_state(facts, "jobs") != "done")
    return (
        *(
            {"op": "launchd-retire", "kind": job.kind, "label": job.label, "plist": job.plist}
            for job in jobs.launchd
        ),
        *({"op": "cron-remove", "kind": line.kind, "line": line.line} for line in jobs.cron),
        *(
            {
                "op": "unit-retire",
                "kind": unit.kind,
                "unit": unit.unit,
                "scope": unit.scope,
                "path": unit.path,
            }
            for unit in jobs.units
        ),
    )


def _hold(facts: Facts) -> tuple[dict[str, Any], ...]:
    owner = facts.pause_owner
    if _ours(facts):
        return ()
    if owner["adoptable"] and facts.journal is None:
        # The legacy stop's own hold becomes the cutover hold: its cohort is
        # exactly the agents that stop drained, which the final resume wakes.
        return (
            {"op": "hold-adopt", "holder": owner["holder"], "acquired_at": owner["acquired_at"]},
        )
    effects: list[dict[str, Any]] = []
    if facts.pause_owner["status"] == "resumed":
        effects.append({"op": "archive", "src": PAUSE_OWNER, "area": "home-files"})
    effects.append({"op": "hold-create", "phase": "stopped"})
    return tuple(effects)


def _files(facts: Facts) -> tuple[dict[str, Any], ...]:
    pidfiles = [pid["path"] for pid in facts.pidfiles if pid["state"] != "live"]
    return tuple(
        {"op": "archive", "src": src, "area": "home-files"}
        for src in sorted(set(facts.legacy_files) | set(pidfiles))
    )


def _selection(facts: Facts) -> tuple[dict[str, Any], ...]:
    if facts.disabled_services is None:
        return ()
    effects: list[dict[str, Any]] = []
    if facts.disabled_services and not (facts.home / SELECTION).exists():
        effects.append({"op": "selection-write", "names": facts.disabled_services})
    effects.append({"op": "archive", "src": DISABLED_SERVICES, "area": "home-files"})
    return tuple(effects)


def _env(facts: Facts, inputs: Inputs) -> tuple[dict[str, Any], ...]:
    removed, sets = env_changes(facts, inputs)
    effects: list[dict[str, Any]] = [
        {"op": "env-set", "key": key, "value": value} for key, value in sorted(sets.items())
    ]
    if removed:
        effects.append({"op": "env-remove", "keys": removed})
    return tuple(effects)


def _record(facts: Facts) -> tuple[dict[str, Any], ...]:
    if facts.record is None:
        return ()
    if facts.gateway:
        if not facts.missing_ports:
            return ()
        return ({"op": "record-complete", "ports": facts.missing_ports},)
    return ({"op": "record-retire", "record": asdict(facts.record)},)


def _intent(facts: Facts) -> tuple[dict[str, Any], ...]:
    if facts.intent_phase is not None:
        return ()
    return ({"op": "intent-write", "phase": "provisioned", "roles": list(facts.roles)},)


def plan(facts: Facts, inputs: Inputs) -> list[Step]:
    """The ordered steps and effects an adoption run would perform now."""
    residue = tuple({"op": "archive", "src": src, "area": "residue"} for src in facts.residue)
    return [
        Step("jobs", _jobs(facts)),
        Step("hold", _hold(facts)),
        Step("files", _files(facts)),
        Step("selection", _selection(facts)),
        Step("env", _env(facts, inputs)),
        Step("residue", residue),
        Step("record", _record(facts)),
        Step("intent", _intent(facts)),
    ]


def intent_document(
    facts: Facts, service_path: str, record: dict[str, Any] | None
) -> dict[str, Any]:
    """Exactly the record a birth would have produced for this identity."""
    env = {key: facts.stored[key] for key in IDENTITY_KEYS if facts.stored.get(key)}
    for cap, key in CAPABILITY_KEYS.items():
        env[key] = str(cap in facts.roles).lower()
    env["AVA_SERVICE_PATH"] = service_path
    return {
        "version": 1,
        "home": str(facts.home),
        "checkout": str(facts.checkout),
        "worktree": False,
        "roles": sorted(facts.roles),
        "config_digest": None,
        "phase": "provisioned",
        "record": record if facts.gateway else None,
        "env": env,
    }


def _service_path_refusals(facts: Facts, inputs: Inputs) -> list[str]:
    from shared.session_env import admit_service_path

    declared = facts.env.get("AVA_SERVICE_PATH")
    if inputs.service_path is None:
        if declared is None:
            return ["AVA_SERVICE_PATH is not declared: pass the reviewed --service-path"]
        return []
    try:
        admitted = admit_service_path(inputs.service_path)
    except ValueError as exc:
        return [f"--service-path: {exc}"]
    if admitted != inputs.service_path:
        return [f"--service-path is not normalized; pass exactly {admitted!r}"]
    if declared is not None and declared != inputs.service_path:
        return ["the home already declares a different AVA_SERVICE_PATH"]
    return []


def _mode_refusals(facts: Facts) -> list[str]:
    if not facts.roles:
        return []
    if facts.gateway:
        reasons = [] if facts.record else ["gateway home has no registry record"]
        missing = [key for key in ("AVA_DB_URL", "AVA_REDIS_URL") if not facts.env.get(key)]
        return reasons + [f"gateway .env has no {key}" for key in missing]
    if not facts.stored.get("AVA_GATEWAY_URL"):
        return ["remote unit has no gateway URL (.env AVA_GATEWAY_URL or gateway_url file)"]
    return []


def _state_refusals(facts: Facts) -> list[str]:
    reasons: list[str] = []
    if facts.intent_phase is not None and step_state(facts, "intent") is None:
        reasons.append("the home already carries a start intent this adoption did not write")
    if (facts.home / "destroy-intent.json").exists():
        reasons.append("the home has a destroy intent")
    owner = facts.pause_owner
    if owner["status"] == "invalid":
        reasons.append(f"{PAUSE_OWNER} is unreadable")
    adoptable = owner["adoptable"] and facts.journal is None
    if owner["status"] == "paused" and not _ours(facts) and not adoptable:
        reasons.append(
            f"{PAUSE_OWNER} holds a pause that is not a completed stop's maintenance hold "
            f"({owner['holder']}, phase {owner['maintenance_phase']})"
        )
    selection = facts.home / SELECTION
    if (
        facts.disabled_services
        and selection.exists()
        and selection.read_bytes() != selection_payload(facts.disabled_services)
    ):
        reasons.append(f"{SELECTION} exists and differs from {DISABLED_SERVICES}")
    return reasons


def _job_refusals(facts: Facts) -> list[str]:
    assert facts.jobs is not None  # noqa: S101 — gathered before planning
    reasons = [f"crontab line nobody can attribute: {line}" for line in facts.jobs.ambiguous]
    if facts.jobs.crontab_error is not None:
        reasons.append(facts.jobs.crontab_error)
    return reasons


def refusals(facts: Facts, inputs: Inputs) -> list[str]:
    """Every reason the adoption must not start (or continue) now; empty means go."""
    reasons = list(facts.problems)
    reasons += _state_refusals(facts)
    reasons += _mode_refusals(facts)
    reasons += _service_path_refusals(facts, inputs)
    reasons += _job_refusals(facts)
    for step in plan(facts, inputs):
        for effect in step.effects:
            if effect["op"] == "archive" and (facts.home / archive_destination(effect)).exists():
                reasons.append(f"archive already holds {archive_destination(effect)}")
    return reasons
