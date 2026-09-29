#!/usr/bin/env python3
"""Read-only inventory of a legacy-born home before the production cutover adopts it.

The fleet cutover converts each existing home into a new-lifecycle home with
`scripts/cutover_adopt_home.py`. This script changes nothing: it reports the
home's legacy state and a machine-readable JSON verdict of exactly what that
adoption would change, computed by the same planner the adoption executes.

Reported facts:

- role (gateway, remote unit, or both on one home) from the persisted
  `machine_serve_*` files and `.env`, the start intent and destroy intent,
  `AVA_SERVICE_PATH`, and a PATH candidate read from a live legacy service
  for the operator to review (never a decision);
- the registry record and port block, the ports the current key set adds, and
  `.env`/record port conflicts;
- inert legacy files (updater, pin and hold residue, session pidfiles) and the
  legacy pause-owner journal;
- on remote units, gateway-only material by name only: `.env` key names, the
  former-gateway residue directories, `secrets/` entry names, the bootstrap
  snapshot. Secret values are never read into the output;
- legacy OS jobs matched by the exact home slug
  (`scripts/cutover_legacy_jobs.py`), and crontab lines nobody can attribute;
- live processes related to the home (cwd, executable, command line or
  `AVA_HOME`), classified as Ava services or other processes, bound ports of
  the home's port block, and the kept permissions helper.

Usage (from the checkout that owns the home):

    .venv/bin/python scripts/cutover_inventory.py --home ~/.ava [--service-path PATH]
    .venv/bin/python scripts/cutover_inventory.py --home ~/.ava --attest ROWS.json

The verdict goes to stdout as JSON. Exit status: 0 when adoption could run now,
2 when it would refuse (the reasons are in `refusals`), 1 on an error.

`--attest ROWS.json` reads a JSON list of legacy process evidence rows
(`{"machine": str, "pid": int, "birth": float, ...}`, as
`scripts/cutover_db_records.py --check --rows-out` exports them) and prints this
machine's one closure attestation: for each of its rows, that no process with
that pid and birth exists or that the host booted after the birth (extra row
fields are echoed back), plus the home's process census and bound ports. The
database-records repair stores the document verbatim and records its sha256
as the machine's closure evidence (`load_attestation`). Exit status 0 when
every row is absent and the census is empty, 2 otherwise; it refuses (1) while
the home's legacy health probe is registered, which can start the old code
after the document is taken.

Both cutover scripts are one-time and are deleted after the cutover.
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import sys
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, cast

import psutil
from dotenv import dotenv_values
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, ValidationError, model_validator

from cli.cutover_hold import ADOPTION_JOURNAL, legacy_hold_facts
from scripts.cutover_legacy_jobs import Host, Jobs, discover
from shared import cluster
from shared.port_block import LEGACY_AVA_PORTS, PORT_OFFSETS

VERSION = 1
ARCHIVE = "cutover-rollback"
_JOURNAL_KEYS = {"version", "home", "cutover_id", "created_at", "inputs", "hold", "steps"}

# Inert legacy files (plan section "Inert files and residue"); directories move whole.
LEGACY_FILES = (
    "installed_sha",
    "deploy-state.json",
    "cluster_paused",
    "health_probe_failures",
    "lgtm-write-probe-consecutive-failures",
    "run/updating.flag",
    "run/gateway-down-since",
    "run/lifecycle-op.json",
    "run/sessions",
    "run/session-code",
    "state/held-stop",
    "bin/ava-boot-converge.sh",
    "logs/boot-converge.state",
)
LEGACY_GLOBS = (
    "deploy-state.*.lock*",
    "run/deploy-state.*.lock*",
    "run/updater*.lock",
    "state/hold-watchdog-attempt*",
)
PAUSE_OWNER = "run/deploy-pause-owner.json"
DISABLED_SERVICES = "disabled_services"
SELECTION = "service-selection.json"
SNAPSHOT = "run/bootstrap-snapshot.json"
# Former-gateway residue on a remote unit (moved aside; the operator archives it offline).
RESIDUE = ("backups", "physical-backup", "redis", "pgbouncer", "pg", "pg-template-17")
RESIDUE_GLOBS = ("masked-backup-*",)
GATEWAY_ONLY_KEYS = (
    "AVA_DB_ADMIN_PASSWORD",
    "AVA_REDIS_ADMIN_PASSWORD",
    "AVA_RUNNER_DB_PASSWORD",
    "AVA_REDIS_PASSWORD",
    "AVA_DB_URL",
    "AVA_REDIS_URL",
)
GATEWAY_ONLY_PREFIXES = ("AVA_PITR_",)
BEARER_KEY = "AVA_CLUSTER_SECRET"
# Keys no current setting declares; `dead_keys` refuses if the table goes stale.
DEAD_KEYS = ("AVA_CLUSTER", "AVA_RESTARTER_HEALTH_PORT", "AVA_TRACK_MODE")
DATA_PLANE_PORTS = ("postgres", "redis", "pgbouncer")

_AVA_NAMES = frozenset(
    {"postgres", "redis-server", "pgbouncer", "otelcol", "otelcol-contrib", "loki"}
    | {"prometheus", "grafana", "grafana-server"}
)
_AVA_TOKENS = ("services.", "shared.sessions", "cli.main", "ava-root", "ava_root")
_CRYPTEX = ("/System/Cryptexes/", "/var/run/com.apple.security.cryptexd/")
_SECRET_WORDS = ("pass", "secret", "token", "key")


class RefusedError(RuntimeError):
    """The home is not in a state this script can attribute or convert."""


# Identity is adopted, never minted; the attestation is keyed by this name too.
NO_MACHINE_NAME = (
    "no persisted machine name: write this unit's name (its `machine_units` row on the "
    "gateway) to $AVA_HOME/machine_name, or declare AVA_MACHINE_NAME in its .env, first"
)


@dataclass(frozen=True)
class Inputs:
    """Operator-supplied adoption inputs; nothing here is inferred from the caller."""

    service_path: str | None = None
    keep_secrets: tuple[str, ...] = ()


@dataclass
class Facts:
    home: Path
    slug: str
    registry: Path
    checkout: Path
    env: dict[str, str | None]
    stored: dict[str, str]
    roles: tuple[str, ...] = ()
    problems: list[str] = field(default_factory=list[str])
    record: cluster.ClusterRecord | None = None
    missing_ports: dict[str, int] = field(default_factory=dict[str, int])
    intent_phase: str | None = None
    pause_owner: dict[str, Any] = field(default_factory=dict[str, Any])
    legacy_files: list[str] = field(default_factory=list[str])
    pidfiles: list[dict[str, Any]] = field(default_factory=list[dict[str, Any]])
    disabled_services: list[str] | None = None
    residue: list[str] = field(default_factory=list[str])
    jobs: Jobs | None = None
    processes: list[dict[str, Any]] = field(default_factory=list[dict[str, Any]])
    listeners: list[dict[str, Any]] = field(default_factory=list[dict[str, Any]])
    service_path_candidate: dict[str, Any] | None = None
    journal: dict[str, Any] | None = None

    @property
    def gateway(self) -> bool:
        return "gateway" in self.roles


def canonical_home(raw: str) -> Path:
    home = Path(raw).expanduser()
    if not home.is_absolute() or home.resolve() != home:
        raise RefusedError(f"--home {raw} must be an absolute canonical path (no symlinks)")
    if not home.is_dir():
        raise RefusedError(f"{home} is not a directory")
    return home


def registry_path(home: Path, explicit: str | None) -> Path:
    """`--registry`, else the home's own `.env` declaration, else the host default."""
    if explicit:
        return Path(explicit).expanduser()
    declared = dotenv_values(home / ".env").get("AVA_CLUSTER_REGISTRY")
    return Path(declared).expanduser() if declared else Path.home() / ".ava" / "clusters.json"


def read_journal(home: Path) -> dict[str, Any] | None:
    """The adoption journal; None before the first `--execute`. Refuses one whose
    completed `intent` step no longer has its start intent: a rollback (R0, R1)
    undid that adoption, and a retry must not read the home as adopted."""
    from cli.start_identity import INTENT_NAME
    from shared.verified_file import regular_bytes

    path = home / ADOPTION_JOURNAL
    try:
        data: object = json.loads(regular_bytes(path))
    except FileNotFoundError:
        return None
    journal = cast("dict[str, Any]", data) if isinstance(data, dict) else {}
    if (
        set(journal) != _JOURNAL_KEYS
        or journal["version"] != VERSION
        or journal["home"] != str(home)
    ):
        raise RefusedError(f"unrecognized adoption journal: {path}")
    intent = journal["steps"].get("intent")
    if intent is not None and intent["state"] == "done" and not (home / INTENT_NAME).exists():
        raise RefusedError(
            f"{path} records an adoption whose {INTENT_NAME} is gone: a rollback (R0, R1) "
            "undid it. Move the journal aside before any attestation or adoption of this home"
        )
    return journal


def _relative_matches(home: Path, patterns: tuple[str, ...]) -> list[str]:
    found = {str(path.relative_to(home)) for pattern in patterns for path in home.glob(pattern)}
    return sorted(found)


def _existing(home: Path, names: tuple[str, ...]) -> list[str]:
    return [name for name in names if (home / name).exists() or (home / name).is_symlink()]


def _roles(facts: Facts) -> None:
    import argparse as _argparse

    from cli.start_intent import _CAP_ARGS, _roles

    for cap, arg in _CAP_ARGS.items():
        key = "AVA_MACHINE_" + arg.upper()
        path = facts.home / f"machine_{arg}"
        declared = facts.env.get(key)
        if declared is not None and path.exists() and path.read_text().strip() != declared:
            facts.problems.append(f"capability {cap}: .env {key} and {path.name} disagree")
    namespace = _argparse.Namespace(
        serve_gateway=None,
        serve_agent_runner=None,
        serve_observability_station=None,
        worktree=False,
    )
    try:
        facts.roles = tuple(sorted(_roles(namespace, facts.stored)))
    except ValueError as exc:
        facts.problems.append(f"roles: {exc}")


def _registry(facts: Facts) -> None:
    from cli.preflight import _port_block_conflicts

    try:
        records = cluster.load_registry(path=facts.registry)
    except (RuntimeError, ValueError, TypeError) as exc:
        facts.problems.append(f"registry {facts.registry}: {exc}")
        return
    facts.record = records.get(str(facts.home))
    if facts.record is None:
        return
    if facts.gateway:
        for conflict in _port_block_conflicts(asdict(facts.record), facts.env):
            facts.problems.append(f"registry/.env port conflict: {conflict}")
        facts.missing_ports = _missing_ports(facts, records)


def _missing_ports(facts: Facts, records: dict[str, cluster.ClusterRecord]) -> dict[str, int]:
    """Ports of the current key set the record lacks, placed by the block rule."""
    record = facts.record
    assert record is not None  # noqa: S101 — caller checked
    ports = dict(cast("dict[str, int]", record.ports))
    base = ports["gateway"] - PORT_OFFSETS["gateway"]
    default = cluster.is_default_home(facts.home)
    missing = {
        key: (LEGACY_AVA_PORTS[key] if default else base + PORT_OFFSETS[key])
        for key in LEGACY_AVA_PORTS
        if key not in ports
    }
    taken = set(ports.values())
    for home, other in records.items():
        if home != str(facts.home):
            taken |= set(cast("dict[str, int]", other.ports).values())
    for key, port in missing.items():
        if not 1024 <= port <= 65535 or port in taken:
            facts.problems.append(f"new port {key}={port} collides with a reserved port")
        taken.add(port)
    return missing


def _pidfiles(facts: Facts) -> None:
    for path in sorted((facts.home / "run").glob("*.pid")):
        raw = path.read_text().strip() if path.is_file() else ""
        state = "invalid"
        pid = int(raw) if raw.isdigit() else None
        if pid is not None:
            state = "stale"
            if psutil.pid_exists(pid):
                related = any(proc["pid"] == pid for proc in facts.processes)
                state = "live" if related else "reused"
        facts.pidfiles.append(
            {"path": str(path.relative_to(facts.home)), "pid": pid, "state": state}
        )
        if state == "live":
            facts.problems.append(f"{path.name} names live home process {pid}")


def _selection(facts: Facts) -> None:
    path = facts.home / DISABLED_SERVICES
    if path.exists():
        names = {line.strip().replace("_", "-") for line in path.read_text().splitlines()}
        facts.disabled_services = sorted(name for name in names if name)


def _residue(facts: Facts, inputs: Inputs) -> None:
    if facts.gateway:
        return
    names = list(RESIDUE)
    if facts.registry.parent == facts.home:
        names.remove("pg-template-17")  # the host-level initdb cache beside the registry
    facts.residue = _existing(facts.home, tuple(names))
    facts.residue += _relative_matches(facts.home, RESIDUE_GLOBS)
    secrets = facts.home / "secrets"
    if secrets.is_dir():
        facts.residue += [
            f"secrets/{entry.name}"
            for entry in sorted(secrets.iterdir())
            if entry.name not in inputs.keep_secrets
        ]
    facts.residue += _existing(facts.home, (SNAPSHOT,))


def env_changes(facts: Facts, inputs: Inputs) -> tuple[list[str], dict[str, str]]:
    """`(keys to remove, keys to set)` for the adopted `.env` (names; values only for sets)."""
    names = [key for key in facts.env if key in DEAD_KEYS]
    if not facts.gateway:
        names += [
            key
            for key in facts.env
            if key in GATEWAY_ONLY_KEYS
            or key.startswith(GATEWAY_ONLY_PREFIXES)
            # A remote unit authenticates with its capability's machine API
            # token; it never holds the human bearer.
            or key == BEARER_KEY
        ]
    sets: dict[str, str] = {}
    if inputs.service_path is not None and facts.env.get("AVA_SERVICE_PATH") != inputs.service_path:
        sets["AVA_SERVICE_PATH"] = inputs.service_path
    if (
        not facts.gateway
        and not facts.env.get("AVA_GATEWAY_URL")
        and facts.stored.get("AVA_GATEWAY_URL")
    ):
        sets["AVA_GATEWAY_URL"] = facts.stored["AVA_GATEWAY_URL"]
    return sorted(set(names)), sets


def dead_keys_are_dead() -> None:
    from shared.config_lite_table import FIELD_ALIASES

    alive = set(DEAD_KEYS) & set(FIELD_ALIASES.values())
    if alive:
        raise RefusedError(f"the dead-key table is stale: {sorted(alive)} are current settings")


def _lineage() -> set[int]:
    pids: set[int] = set()
    process: psutil.Process | None = psutil.Process()
    while process is not None and process.pid not in pids:
        pids.add(process.pid)
        try:
            process = process.parent()
        except psutil.Error:
            break
    return pids


def _under(path: str | None, root: Path) -> bool:
    return bool(path) and Path(str(path)).is_relative_to(root)


def _relations(home: Path, info: dict[str, Any], process: psutil.Process) -> list[str]:
    relations = [
        name for name, value in (("cwd", info["cwd"]), ("exe", info["exe"])) if _under(value, home)
    ]
    argv: list[str] = info["cmdline"] or []
    if any(arg == str(home) or f"{home}/" in arg for arg in argv):
        relations.append("cmdline")
    if not relations:
        try:
            if process.environ().get("AVA_HOME") == str(home):
                relations.append("environment")
        except (psutil.Error, OSError):
            return relations
    return relations


def _kind(facts: Facts, info: dict[str, Any]) -> str:
    argv: list[str] = info["cmdline"] or []
    joined = " ".join(argv)
    if info["name"] in _AVA_NAMES or any(token in joined for token in _AVA_TOKENS):
        return "ava"
    if " -m cli" in f" {joined}" or _under(info["exe"], facts.home):
        return "ava"
    if any(_under(arg, facts.home) or _under(arg, facts.checkout) for arg in argv[:2]):
        return "ava"
    if "--user-data-dir=" in joined and str(facts.home) in joined:
        return "ava"
    return "other"


def _argv_summary(argv: list[str]) -> str:
    shown = [
        arg.split("=", 1)[0] + "=<redacted>"
        if "=" in arg and any(word in arg.split("=", 1)[0].lower() for word in _SECRET_WORDS)
        else arg
        for arg in argv[:4]
    ]
    return " ".join(shown)[:200]


def census(facts: Facts, exempt: set[int]) -> None:
    """Every live process related to the home, except this process's own lineage.

    An ancestor that is itself an Ava process of the home (its PTY host, an
    agent host) refuses instead: the census would skip exactly the process the
    old stop should have ended. The operator's shell (cwd in the home) stays
    exempt.
    """
    lineage = _lineage()
    attrs = ["pid", "name", "cwd", "exe", "cmdline", "create_time"]
    for process in psutil.process_iter(attrs):
        info: dict[str, Any] = process.info  # pyright: ignore[reportAttributeAccessIssue] — psutil sets info
        if info["pid"] in exempt:
            continue
        relations = _relations(facts.home, info, process)
        if not relations:
            continue
        kind = _kind(facts, info)
        if info["pid"] in lineage:
            if info["pid"] != os.getpid() and kind == "ava":
                raise RefusedError(
                    f"this script runs inside Ava process {info['pid']} ({info['name']}) of "
                    f"{facts.home}; run it from a login shell outside the home's processes"
                )
            continue
        facts.processes.append(
            {
                "pid": info["pid"],
                "name": info["name"],
                "create_time": info["create_time"],
                "relations": relations,
                "kind": kind,
                "argv": _argv_summary(info["cmdline"] or []),
            }
        )
        if kind == "ava":
            facts.problems.append(
                f"live home process {info['pid']} ({info['name']}) must stop first"
            )


def _bound(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.3):
            return True
    except ConnectionRefusedError:
        return False
    except OSError:
        return True  # unknown is not absent


def _listeners(facts: Facts) -> None:
    ports = dict(cast("dict[str, int]", facts.record.ports)) if facts.record else {}
    if not ports and cluster.is_default_home(facts.home):
        ports = dict(LEGACY_AVA_PORTS)
    for service, port in sorted(ports.items()):
        if service == "permissions_helper" or not _bound(port):
            continue
        facts.listeners.append({"service": service, "port": port})
        if service in DATA_PLANE_PORTS:
            facts.problems.append(f"data-plane port {service}={port} is bound; stop it first")


def _service_path_candidate(facts: Facts) -> None:
    """The live legacy service PATH minus virtualenvs, the home and injected dirs."""
    for proc in facts.processes:
        if proc["kind"] != "ava" or "services." not in proc["argv"]:
            continue
        try:
            path = psutil.Process(proc["pid"]).environ().get("PATH", "")
        except (psutil.Error, OSError):
            continue
        entries: list[str] = []
        for entry in path.split(":"):
            if not entry or entry in entries or _under(entry, facts.home):
                continue
            if "/.venv/" in f"{entry}/" or entry.startswith(_CRYPTEX):
                continue
            entries.append(entry)
        facts.service_path_candidate = {"source_pid": proc["pid"], "value": ":".join(entries)}
        return


def gather(
    home: Path, registry: Path, checkout: Path, host: Host, inputs: Inputs, *, live: bool = True
) -> Facts:
    """Every fact the verdict and the adoption plan need; read-only."""
    from cli.start_identity import read_intent
    from cli.start_intent import _stored

    env = dict(dotenv_values(home / ".env")) if (home / ".env").exists() else {}
    facts = Facts(home, cluster.home_slug(home), registry, checkout, env, {})
    facts.journal = read_journal(home)
    try:
        intent = read_intent(home)
        facts.intent_phase = intent["phase"] if intent else None
        facts.stored = _stored(home)
    except (RuntimeError, TypeError, ValueError, OSError) as exc:
        facts.problems.append(f"start intent: {exc}")
    _roles(facts)
    _registry(facts)
    facts.pause_owner = legacy_hold_facts(facts.home)
    facts.legacy_files = sorted(
        set(_existing(home, LEGACY_FILES)) | set(_relative_matches(home, LEGACY_GLOBS))
    )
    _selection(facts)
    _residue(facts, inputs)
    facts.jobs = discover(home, host)
    if live:
        helper = {job.pid for job in facts.jobs.launchd if job.kind == "permissions-helper"}
        census(facts, {pid for pid in helper if pid is not None})
        _listeners(facts)
        _service_path_candidate(facts)
    _pidfiles(facts)
    return facts


def verdict(facts: Facts, inputs: Inputs) -> dict[str, Any]:
    """The machine-readable report: facts, refusals and the adoption plan."""
    from scripts.cutover_adopt_plan import plan, refusals

    reasons = refusals(facts, inputs)
    jobs = facts.jobs or Jobs((), (), (), (), None, None)
    removed, sets = env_changes(facts, inputs)
    return {
        "version": VERSION,
        "home": str(facts.home),
        "slug": facts.slug,
        "roles": list(facts.roles),
        "mode": "gateway" if facts.gateway else "remote-unit",
        "start_intent": facts.intent_phase,
        "destroy_intent": (facts.home / "destroy-intent.json").exists(),
        "adoption_journal": _journal_summary(facts.journal),
        "env": {
            "keys": sorted(facts.env),
            "service_path_declared": "AVA_SERVICE_PATH" in facts.env,
            "service_path_candidate": facts.service_path_candidate,
            "remove": removed,
            "set": sorted(sets),
        },
        "registry": {
            "path": str(facts.registry),
            "record": asdict(facts.record) if facts.record else None,
            "missing_ports": facts.missing_ports,
        },
        "legacy_files": facts.legacy_files,
        "pidfiles": facts.pidfiles,
        "pause_owner": facts.pause_owner,
        "disabled_services": facts.disabled_services,
        "residue": facts.residue,
        "jobs": {
            "launchd": [asdict(job) for job in jobs.launchd],
            "cron": [asdict(line) for line in jobs.cron],
            "systemd": [asdict(unit) for unit in jobs.units],
            "ambiguous_cron": list(jobs.ambiguous),
        },
        "processes": facts.processes,
        "listeners": facts.listeners,
        "refusals": reasons,
        "adoptable": not reasons,
        "plan": [step.encode() for step in plan(facts, inputs)],
    }


def _journal_summary(journal: dict[str, Any] | None) -> dict[str, str] | None:
    if journal is None:
        return None
    return {name: step["state"] for name, step in journal["steps"].items()}


# Legacy rows hold `stable_create_time` at write time: the macOS kernel start
# time, or on Linux start ticks plus the `/proc/stat` boot time of that moment,
# which a wall-clock step moves. A live reading this close is the recorded
# process; a wider window only widens the fail-closed `alive`.
_BIRTH_WINDOW_S = 5.0
# A clock step moves the reported boot time as well, so only a birth this far
# before the current boot proves that the boot ended.
_BOOT_MARGIN_S = 300.0


def row_verdict(pid: int, birth: float, boot: float, platform: str = sys.platform) -> str:
    """`absent` or `boot_changed` only when the recorded process is provably gone.

    A live pid is compared through the same primitive the legacy code wrote the
    birth with. Its identity is either confirmed (`alive`), disproved (a birth
    before this boot, or a different macOS kernel start time: a reused pid), or
    left `unknown`: a Linux reading moves with wall-clock steps.
    """
    from shared.native_process.ownership import stable_create_time

    try:
        live = stable_create_time(psutil.Process(pid))
    except psutil.NoSuchProcess:
        return "absent"
    except psutil.Error:
        return "unknown"
    if abs(live - birth) <= _BIRTH_WINDOW_S:
        return "alive"
    if birth < boot - _BOOT_MARGIN_S:
        return "boot_changed"
    return "absent" if platform == "darwin" else "unknown"


def attest(
    rows: list[dict[str, Any]], machine: str, facts: Facts, *, platform: str = sys.platform
) -> dict[str, Any]:
    """This machine's closure attestation: is every recorded `(pid, birth)` of its
    rows provably gone, and does the home's live census show no related process
    and no bound port? One document per machine covers all of its rows."""
    boot = psutil.boot_time()
    attested = [
        {**row, "verdict": row_verdict(int(row["pid"]), float(row["birth"]), boot, platform)}
        for row in rows
        if row["machine"] == machine
    ]
    return {
        "version": VERSION,
        "machine": machine,
        "home": str(facts.home),
        "boot_time": boot,
        "attested_at": datetime.now(UTC).isoformat(),
        "rows": attested,
        "other_machines": sum(1 for row in rows if row["machine"] != machine),
        "all_absent": all(row["verdict"] in ABSENT_VERDICTS for row in attested),
        "processes": facts.processes,
        "listeners": facts.listeners,
        "census_empty": _census_empty(facts.processes, facts.listeners),
    }


ABSENT_VERDICTS = frozenset({"absent", "boot_changed"})


def _census_empty(processes: Sequence[Mapping[str, Any]], listeners: Sequence[object]) -> bool:
    """No bound port and no process related to the home, Ava service or not: a
    survivor of the old stop (an execution root, a terminal child) is user code."""
    return not listeners and not processes


class AttestedRow(BaseModel):
    """One recorded legacy process identity and its verdict (extra fields echoed)."""

    model_config = ConfigDict(extra="allow", frozen=True)

    machine: str
    pid: int = Field(gt=0)
    birth: float = Field(gt=0)
    verdict: Literal["absent", "boot_changed", "alive", "unknown"]


class Attestation(BaseModel):
    """The `--attest` document, validated for the database-records repair."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    version: Literal[1]
    machine: str = Field(min_length=1)
    home: str = Field(min_length=1)
    boot_time: float
    attested_at: AwareDatetime  # when the census was read: the rows it can cover end here
    rows: tuple[AttestedRow, ...]
    other_machines: int
    all_absent: bool
    processes: tuple[dict[str, Any], ...]
    listeners: tuple[dict[str, Any], ...]
    census_empty: bool

    @model_validator(mode="after")
    def consistent(self) -> Attestation:
        if any(row.machine != self.machine for row in self.rows):
            raise ValueError("an attested row names another machine")
        if self.all_absent != all(row.verdict in ABSENT_VERDICTS for row in self.rows):
            raise ValueError("all_absent contradicts the row verdicts")
        if self.census_empty != _census_empty(self.processes, self.listeners):
            raise ValueError("census_empty contradicts the recorded processes and listeners")
        return self

    @property
    def proves_closure(self) -> bool:
        """Every recorded process is gone and the home census is empty."""
        return self.all_absent and self.census_empty

    def absent(self) -> frozenset[tuple[int, float]]:
        return frozenset(
            (row.pid, row.birth) for row in self.rows if row.verdict in ABSENT_VERDICTS
        )


def load_attestation(path: Path) -> tuple[Attestation, bytes]:
    """The validated document and its exact bytes (whose sha256 is the evidence)."""
    raw = path.read_bytes()
    try:
        return Attestation.model_validate_json(raw), raw
    except ValidationError as exc:
        raise RefusedError(f"{path} is not a closure attestation: {exc}") from exc


def persisted_machine(home: Path) -> str:
    """The machine name the home persisted (`.env` or `machine_name`); never minted."""
    from cli.start_intent import _stored

    machine = _stored(home).get("AVA_MACHINE_NAME")
    if not machine:
        raise RefusedError(NO_MACHINE_NAME)
    return machine


def _require_disarmed(facts: Facts) -> None:
    """An attestation of a home whose legacy health probe is armed proves nothing
    lasting: the probe can start the old code once the document is taken."""
    from scripts.cutover_adopt_plan import step_state

    assert facts.jobs is not None  # noqa: S101 — gathered before attesting
    probe = facts.jobs.health_probe_refusal()
    if probe is not None and step_state(facts, "jobs") is None:
        raise RefusedError(probe)


def own_checkout() -> Path:
    return Path(__file__).resolve().parents[1]


def main(argv: list[str] | None = None, *, host: Host | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument("--home", required=True, help="the home to inventory (explicit)")
    parser.add_argument("--registry", help="cluster registry (default: the home's, else ~/.ava)")
    parser.add_argument("--service-path", help="the AVA_SERVICE_PATH adoption would record")
    parser.add_argument("--keep-secret", action="append", default=[], help="secrets/ entry to keep")
    parser.add_argument("--attest", help="JSON rows of legacy process evidence to attest")
    args = parser.parse_args(argv)
    try:
        home = canonical_home(args.home)
        inputs = Inputs(args.service_path, tuple(args.keep_secret))
        registry = registry_path(home, args.registry)
        facts = gather(home, registry, own_checkout(), host or Host.current(), inputs)
        if args.attest:
            _require_disarmed(facts)
            rows = json.loads(Path(args.attest).read_text())
            report = attest(rows, persisted_machine(home), facts)
            print(json.dumps(report, indent=2, sort_keys=True))
            return 0 if report["all_absent"] and report["census_empty"] else 2
        report = verdict(facts, inputs)
    except (RefusedError, RuntimeError, ValueError, OSError, KeyError) as exc:
        print(f"cutover inventory failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report["adoptable"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
