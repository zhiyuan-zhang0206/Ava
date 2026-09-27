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
(`{"machine": str, "pid": int, "birth": float, ...}`, as the database-records
check exports them) and attests, for the rows of this machine, that no process
with that pid and birth exists, or that the host booted after the birth. Extra
row fields are echoed back. Exit status 0 when every row is absent, 2 otherwise.

Both cutover scripts are one-time and are deleted after the cutover.
"""

from __future__ import annotations

import argparse
import json
import socket
import sys
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import psutil
from dotenv import dotenv_values

from scripts.cutover_legacy_jobs import Host, Jobs, discover
from shared import cluster
from shared.port_block import LEGACY_AVA_PORTS, PORT_OFFSETS

VERSION = 1
ARCHIVE = "cutover-rollback"
JOURNAL = f"{ARCHIVE}/adopt-home.json"
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
DEAD_KEYS = ("AVA_CLUSTER", "AVA_RESTARTER_HEALTH_PORT")
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


@dataclass(frozen=True)
class Inputs:
    """Operator-supplied adoption inputs; nothing here is inferred from the caller."""

    service_path: str | None = None
    retire_bearer: bool = False
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
    from shared.verified_file import regular_bytes

    path = home / JOURNAL
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


def _pause_owner(facts: Facts) -> None:
    from shared import pause_owner

    snapshot = pause_owner.read_for_home(facts.home)
    hold = snapshot.maintenance
    facts.pause_owner = {
        "status": snapshot.status,
        "holder": snapshot.holder,
        "acquired_at": snapshot.acquired_at.isoformat() if snapshot.acquired_at else None,
        "maintenance_phase": hold.phase if hold else None,
        "cohort": sorted(hold.commands) if hold else [],
        # The completed legacy `ava stop` leaves exactly this hold; adoption keeps it.
        "adoptable": snapshot.status == "paused"
        and hold is not None
        and hold.phase == "stopped"
        and not hold.unsettled_failures(),
    }


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
            if key in GATEWAY_ONLY_KEYS or key.startswith(GATEWAY_ONLY_PREFIXES)
        ]
        if inputs.retire_bearer and BEARER_KEY in facts.env:
            names.append(BEARER_KEY)
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
    """Every live process related to the home, except this process's own lineage."""
    skip = _lineage() | exempt
    attrs = ["pid", "name", "cwd", "exe", "cmdline", "create_time"]
    for process in psutil.process_iter(attrs):
        info: dict[str, Any] = process.info  # pyright: ignore[reportAttributeAccessIssue] — psutil sets info
        if info["pid"] in skip:
            continue
        relations = _relations(facts.home, info, process)
        if not relations:
            continue
        kind = _kind(facts, info)
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
    _pause_owner(facts)
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


def attest(rows: list[dict[str, Any]], machine: str) -> dict[str, Any]:
    """For this machine's rows: is every recorded `(pid, birth)` provably gone?"""
    boot = psutil.boot_time()
    attested: list[dict[str, Any]] = []
    for row in rows:
        if row["machine"] != machine:
            continue
        pid, birth = int(row["pid"]), float(row["birth"])
        if birth < boot:
            state = "boot_changed"
        else:
            try:
                alive = abs(psutil.Process(pid).create_time() - birth) < 0.01
                state = "alive" if alive else "absent"
            except psutil.NoSuchProcess:
                state = "absent"
            except psutil.Error:
                state = "unknown"
        attested.append({**row, "verdict": state})
    return {
        "version": VERSION,
        "machine": machine,
        "boot_time": boot,
        "attested_at": datetime.now(UTC).isoformat(),
        "rows": attested,
        "other_machines": sum(1 for row in rows if row["machine"] != machine),
        "all_absent": all(row["verdict"] in {"absent", "boot_changed"} for row in attested),
    }


def own_checkout() -> Path:
    return Path(__file__).resolve().parents[1]


def main(argv: list[str] | None = None, *, host: Host | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument("--home", required=True, help="the home to inventory (explicit)")
    parser.add_argument("--registry", help="cluster registry (default: the home's, else ~/.ava)")
    parser.add_argument("--service-path", help="the AVA_SERVICE_PATH adoption would record")
    parser.add_argument("--retire-bearer", action="store_true", help="plan bearer removal")
    parser.add_argument("--keep-secret", action="append", default=[], help="secrets/ entry to keep")
    parser.add_argument("--attest", help="JSON rows of legacy process evidence to attest")
    args = parser.parse_args(argv)
    try:
        home = canonical_home(args.home)
        if args.attest:
            from cli.start_intent import _stored

            rows = json.loads(Path(args.attest).read_text())
            report = attest(rows, _stored(home)["AVA_MACHINE_NAME"])
            print(json.dumps(report, indent=2, sort_keys=True))
            return 0 if report["all_absent"] else 2
        inputs = Inputs(args.service_path, args.retire_bearer, tuple(args.keep_secret))
        registry = registry_path(home, args.registry)
        facts = gather(home, registry, own_checkout(), host or Host.current(), inputs)
        report = verdict(facts, inputs)
    except (RefusedError, RuntimeError, ValueError, OSError, KeyError) as exc:
        print(f"cutover inventory failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report["adoptable"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
