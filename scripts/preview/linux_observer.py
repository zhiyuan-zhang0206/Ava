"""Read-only native evidence for a disposable four-service Linux preview.

Run from the candidate checkout with its private home and registry environment.
The only written artifact is cycle-LABEL.json; failed checks retain partial
evidence and exit nonzero. Manager stop preserves data and custodied terminals.
"""

from __future__ import annotations

import argparse
import dataclasses
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Literal, cast
from urllib.parse import urlsplit

import psutil
from dotenv import dotenv_values

from scripts.preview.linux_runtime import ExpectedRuntime, environment_digest, expected_runtime
from scripts.preview.linux_terminals import (
    observe_terminals,
    process_observation,
    recorded_members,
    retained_members,
)
from scripts.preview.runtime import SERVICES, owned_processes
from shared.native_process.ownership import OwnedProcess, capture_tree
from shared.root_control.client import RootClient, native_identity

Mode = Literal["running", "manager-running", "stopped", "manager-stopped", "destroyed"]
MODES = ("running", "manager-running", "stopped", "manager-stopped", "destroyed")
Report = dict[str, Any]
Births = dict[str, dict[str, Any]]
_DATA_SERVICES = frozenset({"postgres", "redis", "pgbouncer"})


def _require(condition: object, detail: str) -> None:
    if not condition:
        raise RuntimeError(detail)


def _require_context(run: Path) -> None:
    """Refuse other homes before importing Settings or contacting storage."""
    _require(sys.platform == "linux", "the native cycle observer requires Linux")
    _require(
        Path(__file__).resolve().parents[2] == (run / "source").resolve(),
        "observer must run from this preview's candidate source",
    )
    for key, expected in (
        ("AVA_HOME", run / "home"),
        ("AVA_CLUSTER_REGISTRY", run / "clusters.json"),
    ):
        actual = os.environ.get(key)
        _require(actual and Path(actual).resolve() == expected, f"{key} names another preview")


def _capture(pid: int) -> dict[str, Any]:
    return dataclasses.asdict(OwnedProcess.capture(psutil.Process(pid)))


def _digest(path: Path) -> str | None:
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None


def _command(argv: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 — fixed read-only native commands, no shell
        argv, capture_output=True, text=True, timeout=10, check=False
    )


def _base_observations(run: Path, result: Report) -> dict[str, int]:
    from shared.cluster import load_registry
    from shared.os_boot_unit import unit_name, unit_path
    from shared.port_preflight import strict_listeners_on

    home, source = run / "home", run / "source"
    config = json.loads((run / "config.json").read_text())
    ports: dict[str, int] = config["ports"]
    _require(
        ports.keys() >= _DATA_SERVICES
        and all(type(port) is int and 0 < port < 65536 for port in ports.values()),
        "preview configuration lacks valid native data-plane ports",
    )
    commit = _command(["git", "-C", str(source), "rev-parse", "HEAD"])
    commit.check_returncode()
    result.update(source_commit=commit.stdout.strip(), ports=ports)
    paths = [
        home / name
        for name in (".env", "start-intent.json", "service-selection.json", "installed.json")
    ]
    paths.extend((run / "clusters.json", source / ".ava_home"))
    result["hashes"] = {str(path.relative_to(run)): _digest(path) for path in paths}
    result["registry_contains_home"] = str(home) in load_registry(path=run / "clusters.json")
    result["listeners"] = {
        name: [_capture(pid) for pid in sorted(strict_listeners_on(port))]
        for name, port in ports.items()
    }
    observations: list[Report] = []
    result["owned_processes"] = observations
    for process in owned_processes(run):
        observations.append(process_observation(process))
    state = _command(
        [
            "systemctl",
            "show",
            "--property=MainPID,ControlPID,ControlGroup,ActiveState,SubState,Result,LoadState",
            unit_name(home),
        ]
    )
    manager: Report = dict(row.split("=", 1) for row in state.stdout.splitlines() if "=" in row)
    manager["returncode"] = state.returncode
    result["manager"] = manager
    result["unit_exists"] = unit_path(home).exists()
    result["helper_artifact_exists"] = (run / "helper-artifact").exists()
    result["helper_sockets"] = [
        str(path) for path in (home / "run").glob("permissions-helper.*.sock")
    ]
    _require(
        not result["helper_artifact_exists"] and not result["helper_sockets"],
        "Linux preview contains macOS helper artifacts",
    )
    return ports


def _require_private_endpoint(url: str, port: int) -> None:
    endpoint = urlsplit(url)
    _require(
        endpoint.hostname in {"localhost", "127.0.0.1", "::1"} and endpoint.port == port,
        "storage observation endpoint is outside this preview's reserved loopback port",
    )


def _data_births(home: Path, ports: dict[str, int]) -> Births:
    import redis

    from shared.cluster import ownership, redis_admin_url

    pg = ownership.postgres(home / "pg")
    pool = ownership.pooler(home / "pgbouncer/pgbouncer.ini", home / "pgbouncer/pgbouncer.pid")
    url = redis_admin_url()
    _require_private_endpoint(url, ports["redis"])
    with redis.Redis.from_url(
        url,
        decode_responses=True,
        single_connection_client=True,
        socket_connect_timeout=5,
        socket_timeout=5,
    ) as client:
        info = client.info("server")
        directory = client.config_get("dir")
        cache = ownership.redis_server(info, directory, home / "redis")
    if pg is None or pool is None:
        raise RuntimeError("preview lost PostgreSQL or PgBouncer ownership")
    values = {"postgres": pg, "redis": cache, "pgbouncer": pool}
    for name, owner in values.items():
        ownership.require_listener(owner, ports[name])
    return {name: dataclasses.asdict(owner) for name, owner in values.items()}


def _stored_agents(run: Path, ports: dict[str, int]) -> list[int]:
    import psycopg

    from shared.config import settings

    agents = sorted(
        {int(json.loads(path.read_text())["agent"]) for path in run.glob("smoke-*.json")}
    )
    _require_private_endpoint(settings.data_plane.db_url, ports["pgbouncer"])
    with psycopg.connect(
        settings.data_plane.db_url,
        connect_timeout=5,
    ) as connection:
        # PgBouncer ignores libpq options. Set the transaction posture on the
        # actual borrowed backend; the deadline disappears with this transaction.
        connection.read_only = True
        connection.execute("SET LOCAL statement_timeout = '5s'")
        present = [
            int(row[0])
            for row in connection.execute(
                "SELECT id FROM agents WHERE id = ANY(%s) ORDER BY id", (agents,)
            )
        ]
    _require(present == agents, f"persisted agents differ: present={present}, expected={agents}")
    return present


def _root_births(body: Report) -> Births:
    records: Births = {}
    for name, row in [("root", body["root"]), *((row["id"], row) for row in body["units"])]:
        _require(name == "root" or row["state"] == "running", f"root unit is not running: {name}")
        owner = native_identity(row)
        identity = dataclasses.asdict(owner)
        _require(
            owner.live() and owner.same_birth(OwnedProcess(**_capture(owner.pid))),
            f"native birth changed: {name}",
        )
        records[name] = identity
    _require(
        frozenset(records) == SERVICES | {"root"}, f"unexpected root roster: {sorted(records)}"
    )
    return records


def _observe_path(
    run: Path, root: dict[str, Any], result: Report, runtime: ExpectedRuntime
) -> None:
    declared = dotenv_values(run / "home/.env", interpolate=False)["AVA_SERVICE_PATH"]
    if not declared:
        raise RuntimeError("preview has no admitted host execution PATH")
    process = psutil.Process(root["pid"])
    native_env = process.environ()
    result["root_native"] = native = {
        "argv": process.cmdline(),
        "cwd": process.cwd(),
        "executable": process.exe(),
        "environment_sha256": environment_digest(native_env),
    }
    _require(native["argv"] == runtime.argv(run), "root argv differs from expected runtime")
    _require(native["cwd"] == str(runtime.cwd), "root cwd differs from expected runtime")
    _require(
        native["executable"] == str(runtime.interpreter.resolve(strict=True)),
        "root executable differs from expected runtime",
    )
    _require(
        not {"PYTHONPATH", "PYTHONHOME"}.intersection(native_env),
        "root has foreign Python path overrides",
    )
    for key, value in runtime.environment(run, declared).items():
        _require(native_env.get(key) == value, f"root changed expected runtime environment: {key}")
    result["service_path"] = {
        "declared": declared,
        "native": native_env["AVA_SERVICE_PATH"],
        "root_path": native_env["PATH"],
    }
    _require(native_env["AVA_SERVICE_PATH"] == declared, "root changed the admitted host PATH")
    entries = native_env["PATH"].split(":")
    _require(entries[0] == str(runtime.interpreter.parent), "root uses another runtime PATH")
    _require(len(entries) == len(set(entries)), "root PATH contains duplicate directories")
    _require(OwnedProcess(**root).live(), "root birth changed during environment observation")


def _observe_health(body: Report, root: dict[str, Any], result: Report) -> None:
    diagnostic = body["health"]["diagnostic:redis-acl"]
    result["redis_acl_diagnostic"] = diagnostic
    _require(
        diagnostic["expected"] is True and diagnostic["last_verdict"] == "alive",
        "Redis ACL diagnostic is not an expected healthy sample",
    )
    _require(diagnostic["sampled_at"] >= root["birth"], "Redis diagnostic predates root birth")
    completed = body["health"]["observer:root-health"]["last_completed_at"]
    _require(completed >= diagnostic["sampled_at"], "Redis sample has no completed health round")


def _cgroup(pid: int) -> str:
    return Path(f"/proc/{pid}/cgroup").read_text()


def _require_owned_listeners(owner: OwnedProcess, listeners: list[OwnedProcess], port: int) -> None:
    from shared.port_preflight import strict_listeners_on

    _require(
        listeners
        and {item.birth_key() for item in listeners}
        <= {item.birth_key() for item in capture_tree(owner)},
        "listener is outside its application tree",
    )
    _require(
        set(strict_listeners_on(port)) == {listener.pid for listener in listeners}
        and all(listener.live() for listener in listeners),
        "listener birth changed during observation",
    )


def _observe_app_ownership(records: Births, ports: dict[str, int], result: Report) -> None:
    root = OwnedProcess(**records["root"])
    group = _cgroup(root.pid)
    result["root_cgroup"] = group
    details: Report = {}
    result["unit_native"] = details
    port_names = {"frontend": "app", "agent-host": "agent_host"}
    for name in sorted(SERVICES):
        owner = OwnedProcess(**records[name])
        process = psutil.Process(owner.pid)
        detail = {
            "ppid": process.ppid(),
            "cgroup": _cgroup(owner.pid),
            "cmdline": process.cmdline(),
        }
        details[name] = detail
        _require(
            detail["ppid"] == root.pid and detail["cgroup"] == group,
            f"{name} is outside the captured root tree",
        )
        port_name = port_names.get(name, name)
        listeners = [OwnedProcess(**row) for row in result["listeners"][port_name]]
        _require_owned_listeners(owner, listeners, ports[port_name])
        _require(root.live() and owner.live(), f"{name} or root birth changed during observation")


def _observe_running(
    run: Path, mode: Mode, ports: dict[str, int], result: Report, runtime: ExpectedRuntime
) -> None:
    from shared.os_boot_unit import unit_name, unit_path

    home = run / "home"
    response = RootClient(home / "run/ava-root/ava-root.sock", timeout=5).status()
    _require(response["ok"], f"root status refused: {response}")
    if "result" not in response:
        raise RuntimeError("root status omitted its snapshot")
    body = cast("Report", response["result"])
    result["root_status"] = body
    result["births"] = records = _root_births(body)
    root = records["root"]
    result["root_ppid"] = psutil.Process(root["pid"]).ppid()
    _require(result["root_ppid"] == 1, "root has not transferred to PID 1")
    _observe_path(run, root, result, runtime)
    _observe_health(body, root, result)
    _observe_app_ownership(records, ports, result)
    observe_terminals(home, result)
    result["data_births"] = _data_births(home, ports)
    result["stored_agents"] = _stored_agents(run, ports)
    _require(
        result["registry_contains_home"] and result["hashes"]["source/.ava_home"],
        "running preview lost its registry or checkout pointer",
    )
    if mode == "manager-running":
        _require(
            result["manager"]["returncode"] == 0
            and result["manager"]["MainPID"] == str(root["pid"])
            and result["manager"]["ActiveState"] == "active",
            "systemd does not own the live root",
        )
        _require(
            f"/system.slice/{unit_name(home)}" in result["root_cgroup"],
            "root is outside its systemd cgroup",
        )
        result["unit_text"] = unit_path(home).read_text()
        _require(
            f"AVA_CLUSTER_REGISTRY={run}/clusters.json" in result["unit_text"],
            "systemd unit names another registry",
        )


def _prior_reports(run: Path) -> list[tuple[Path, Report]]:
    return [(path, json.loads(path.read_text())) for path in sorted(run.glob("cycle-*.json"))]


def _root_lock_free(path: Path) -> bool:
    import fcntl

    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        held, current = os.fstat(fd), path.stat()
        _require(
            (held.st_dev, held.st_ino) == (current.st_dev, current.st_ino),
            "root lock changed during observation",
        )
        fcntl.flock(fd, fcntl.LOCK_UN)
        return True
    finally:
        os.close(fd)


def _closed_apps(run: Path, result: Report) -> None:
    retained = [
        {"source": path.name, "unit": name, "identity": row}
        for path, earlier in _prior_reports(run)
        for name, row in earlier.get("births", {}).items()
        if OwnedProcess(**row).live()
    ]
    root_dir = run / "home/run/ava-root"
    result["retained_app_births"] = retained
    result["root_socket_exists"] = (root_dir / "ava-root.sock").exists()
    result["custody"] = [str(path) for path in (root_dir / "custody").glob("*")]
    result["root_lock_free"] = _root_lock_free(root_dir / "ava-root.lock")
    _require(
        not retained and not result["root_socket_exists"] and not result["custody"],
        "application births, socket, or custody remain after stop",
    )


def _manager_stopped(run: Path, ports: dict[str, int], result: Report) -> None:
    result["data_births"] = _data_births(run / "home", ports)
    result["stored_agents"] = _stored_agents(run, ports)
    previous = json.loads((run / "cycle-manager-initial.json").read_text())
    _require(
        {name: OwnedProcess(**row).birth_key() for name, row in result["data_births"].items()}
        == {name: OwnedProcess(**row).birth_key() for name, row in previous["data_births"].items()},
        "manager stop replaced a native data-plane birth",
    )
    _require(
        result["manager"]["returncode"] == 0
        and result["manager"]["ActiveState"] == "inactive"
        and result["manager"]["MainPID"] == "0",
        "systemd still owns an active root",
    )
    _require(
        all(not rows for name, rows in result["listeners"].items() if name not in _DATA_SERVICES),
        "an application listener remains after manager stop",
    )
    before_stop = json.loads((run / "cycle-manager-repeat.json").read_text())
    observe_terminals(run / "home", result)
    terminal_members = retained_members(result["terminals"], before_stop["terminals"])
    _require_only_resource_survivors(result, terminal_members)


def _require_only_resource_survivors(result: Report, terminal_members: set[OwnedProcess]) -> None:
    allowed = set(terminal_members)
    for row in result["data_births"].values():
        owner = OwnedProcess(**row)
        allowed.update(capture_tree(owner))
        _require(owner.live(), "data owner exited during descendant observation")
    unexpected = [
        row
        for row in result["owned_processes"]
        if not row.get("exited_during_observation", False)
        and (identity := OwnedProcess(**row["identity"])).live()
        and identity.birth_key() not in {item.birth_key() for item in allowed}
    ]
    result["unexpected_survivors"] = unexpected
    _require(
        not unexpected,
        "private processes outside verified data/terminal custody survived manager stop",
    )


def _closed_terminals(run: Path, result: Report) -> None:
    observe_terminals(run / "home", result)
    _require(not result["terminals"], "custodied terminals remain after full stop")
    result["retained_terminal_births"] = terminal_births = [
        dataclasses.asdict(identity)
        for _, earlier in _prior_reports(run)
        for row in earlier.get("terminals", {}).values()
        for identity in recorded_members(row)
        if identity.live()
    ]
    _require(not terminal_births, "captured terminal births remain alive after full stop")


def _observe_stopped(run: Path, mode: Mode, ports: dict[str, int], result: Report) -> None:
    _closed_apps(run, result)
    if mode == "manager-stopped":
        _manager_stopped(run, ports, result)
        return
    _require(
        not result["owned_processes"] and not any(result["listeners"].values()),
        "preview-owned processes or listeners remain after full stop",
    )
    _closed_terminals(run, result)
    result["retained_data_births"] = retained = [
        row
        for _, earlier in _prior_reports(run)
        for row in earlier.get("data_births", {}).values()
        if OwnedProcess(**row).live()
    ]
    _require(not retained, "captured data-plane births remain alive after full stop")
    if mode == "destroyed":
        pointer = run / "source/.ava_home"
        _require(
            not result["registry_contains_home"]
            and not pointer.exists()
            and not pointer.is_symlink(),
            "destroy retained the registry or checkout binding",
        )
        _require(
            not result["unit_exists"] and result["manager"]["LoadState"] == "not-found",
            "destroy retained a systemd unit",
        )
    else:
        _require(
            result["registry_contains_home"] and result["hashes"]["source/.ava_home"],
            "ordinary stop removed the registry or checkout binding",
        )


def observe(run: Path, label: str, mode: Mode, *, runtime_receipt: Path | None = None) -> Report:
    """Record the independently observed state, including every failed check."""
    if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,79}", label) or mode not in MODES:
        raise ValueError("invalid cycle label or observation mode")
    run = run.resolve(strict=True)
    result: Report = {"label": label, "mode": mode, "at": time.time()}
    try:
        _require_context(run)
        runtime = expected_runtime(run, runtime_receipt)
        result["runtime"] = runtime.evidence or {
            "kind": "source",
            "interpreter": str(runtime.interpreter),
            "cwd": str(runtime.cwd),
        }
        ports = _base_observations(run, result)
        if mode in {"running", "manager-running"}:
            _observe_running(run, mode, ports, result, runtime)
        else:
            _observe_stopped(run, mode, ports, result)
        result["result"] = "passed"
        return result
    except BaseException as error:
        result.update(result="failed", error=repr(error))
        raise
    finally:
        snapshot = f"cycle-{label}.json"
        (run / snapshot).write_text(json.dumps(result, indent=2) + "\n")
        print(
            json.dumps(
                {
                    "snapshot": snapshot,
                    **{
                        key: result.get(key) for key in ("result", "error", "births", "data_births")
                    },
                }
            ),
            flush=True,
        )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run", type=Path)
    parser.add_argument("label")
    parser.add_argument("mode", choices=MODES)
    parser.add_argument("--runtime-receipt", type=Path)
    args = parser.parse_args()
    observe(args.run, args.label, cast("Mode", args.mode), runtime_receipt=args.runtime_receipt)


if __name__ == "__main__":
    main()
