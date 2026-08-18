"""Port-preflight helpers — the expected-port set, the bind probe, and drift.

The `ava start` preflight (cli/commands/_port_preflight.py), the `ava stop`
orphan sweep (cli/commands/stop.py) and the rollout's gateway-readiness gate
(cli/commands/_gateway_ready.py) all need the same three facts about a
cluster's ports, stated here beside the record code they read:

- `expected_cluster_ports` / `unit_port_map` — the service->port map the
  cluster / this unit expects to own (registry record, or the legacy block for
  a record-less default home; `unit_port_map` adds the per-unit health ports);
- `occupied_ports` / `listeners_on` — the ports currently bound on this host
  and the pids holding them (a bind probe, with an optional `is_ours` filter
  so an idempotent restart's own daemons do not read as conflicts);
- `process_mentions` / `listener_is_ours` — the pid ownership predicate (the
  #1603/#1606 lineage): a listener counts as this unit's when its argv,
  resolved executable, or working directory mentions the unit's repo or home
  path — the same rule `ava stop`'s orphan sweep and the readiness gate apply
  to tell "ours, safe to kill / safe to bless" from "foreign, never touch";
- `env_port_drift` — `.env` port values that disagree with the registry record,
  the silent drift `shared.port_block`'s docstring warns about.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from pathlib import Path
from typing import cast
from urllib.parse import urlsplit

from shared.cluster import (
    LEGACY_AVA_PORTS,
    ClusterPorts,
    ClusterRecord,
    _port_free,
    get_record,
    record_app_port,
    record_health_port,
    record_pgbouncer_port,
)
from shared.port_block import PORT_OFFSETS


def expected_cluster_ports(home: Path) -> ClusterPorts:
    """The full service->port map this cluster expects to own on THIS host.

    The registry record's block when the cluster has one — missing keys derived
    exactly as `record_health_port` derives them (a record saved before a slot
    existed still resolves deterministically) — else the fixed legacy block: the
    pre-registry default home's ports ARE its record, and a home with no record
    binds them.

    The start preflight bind-checks every port in this map and warns on foreign
    occupants; `record_health_port` is the single derivation rule so the check
    can never disagree with what `derive_env` wrote into the unit's `.env`."""
    rec = get_record(home)
    if rec is None:
        # LEGACY_AVA_PORTS moved to the dependency-free port_block module as a
        # plain dict (shared.cluster cannot be imported here); it IS the legacy
        # ClusterPorts shape.
        return cast("ClusterPorts", LEGACY_AVA_PORTS)
    return cast("ClusterPorts", {svc: record_health_port(rec, svc) for svc in PORT_OFFSETS})


def occupied_ports(
    ports: Mapping[str, int],
    *,
    is_ours: Callable[[int], bool] | None = None,
) -> list[tuple[str, int]]:
    """(service, port) pairs among `ports` that something on this host is bound to.

    A bind probe answers "is anyone listening", which on an idempotent restart is
    the cluster's OWN daemons — `is_ours` lets the caller exempt the listeners it
    can identify as its own, so a healthy restart does not read as a conflict.
    None = every occupant counts (the install-time allocator's view)."""
    out: list[tuple[str, int]] = []
    for svc, port in ports.items():
        if _port_free(port):
            continue
        if is_ours is not None and is_ours(port):
            continue
        out.append((svc, port))
    return out


def env_port_drift(home: Path, rec: ClusterRecord) -> list[str]:
    """One line per `.env` port value that disagrees with the registry record.

    `derive_env` writes both sides at birth, so a later disagreement is a hand
    edit that drifted from the block the record owns: the unit binds the `.env`
    value while the registry still claims the record's ports, and a future
    install on this host can hand those to someone else (the silent-drift risk
    `shared.port_block`'s docstring warns about).

    Only keys PRESENT in the `.env` are compared — an absent key is a unit that
    never got one, not a drift. Health ports are deliberately excluded: they are
    a per-UNIT fact (`ava enroll --health-port-base` moves them on purpose), and
    the pre-bind gate already guards them. AVA_DB_URL's expected port follows
    the unit's own AVA_PGBOUNCER_ENABLED (pooler when on, direct pg when off)."""
    from dotenv import dotenv_values

    env = dotenv_values(home / ".env")
    expected_ports = {
        "AVA_GATEWAY_PORT": record_health_port(rec, "gateway"),
        "AVA_APP_PORT": record_app_port(rec),
        "AVA_MILVUS_PORT": record_health_port(rec, "milvus"),
        "AVA_BROWSER_CDP_PORT": record_health_port(rec, "browser"),
        "AVA_NATIVE_HELPER_PORT": record_health_port(rec, "permissions_helper"),
        "AVA_PERMISSIONS_HELPER_PORT": record_health_port(rec, "permissions_helper"),
    }
    out: list[str] = []
    for key, expected in expected_ports.items():
        value = env.get(key)
        if value is not None and value != str(expected):
            out.append(f"{key}: .env={value!r} vs registry={expected}")
    # AVA_DB_URL is the one access URL: its expected port is the pooler listener
    # when this unit's own .env enables pooling (AVA_PGBOUNCER_ENABLED, default
    # on), else the direct Postgres port. The pooler port is a registry fact now
    # (no AVA_PGBOUNCER_PORT key) — a URL carrying the OTHER port of this cluster
    # is the pre-cutover / kill-switch drift converge normalizes.
    db_expected = (
        record_pgbouncer_port(rec)
        if (env.get("AVA_PGBOUNCER_ENABLED") or "true").strip().lower()
        in ("1", "true", "yes", "on")
        else record_health_port(rec, "postgres")
    )
    for key, expected in (
        ("AVA_DB_URL", db_expected),
        ("AVA_REDIS_URL", record_health_port(rec, "redis")),
    ):
        value = env.get(key)
        if not value:
            continue
        try:
            port = urlsplit(value).port
        except ValueError:
            continue
        if port is not None and port != expected:
            out.append(f"{key}: url port={port} vs registry={expected}")
    return out


# ── listener / ownership predicates (the #1603/#1606 lineage) ─────────────────
# Moved here from cli/commands/_port_preflight.py so `ava stop`'s orphan sweep
# and the rollout's gateway-readiness gate reuse the SAME ownership rule the
# start preflight applies — one definition of "this unit's process", three
# consumers (Task #965).


def listeners_on(port: int) -> list[int]:
    """PIDs listening on `port` (any interface), best-effort.

    psutil is the primary scan and works on Linux/Windows. On macOS it raises
    `AccessDenied` the moment ANY process's socket info is unreadable (a
    root-owned or other-user process in the scan), so there we fall back to
    `lsof -iTCP:<port> -sTCP:LISTEN`, which skips what it cannot read. An empty
    result makes the caller treat the occupant as foreign — conservative: a
    warning costs nothing, a hidden conflict costs a start."""
    import psutil

    try:
        conns = psutil.net_connections(kind="tcp")
    except (psutil.Error, OSError):
        conns = None
    if conns is not None:
        pids: list[int] = []
        for conn in conns:
            if conn.status != "LISTEN" or conn.pid is None:
                continue
            addr = conn.laddr
            if len(addr) < 2:
                continue
            if addr[1] == port:  # psutil addr = (ip, port); `addr.port` is
                pids.append(conn.pid)  # un-narrowable against `tuple[()]`
        return pids

    import subprocess

    try:
        # S603: static argv; the only interpolated piece is an int port.
        out = subprocess.run(  # noqa: S603
            ["lsof", "-nP", "-Fp", "-sTCP:LISTEN", f"-iTCP:{port}"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    pids = []
    for line in out.stdout.splitlines():
        if line.startswith("p"):
            try:
                pids.append(int(line[1:]))
            except ValueError:
                continue
    return list(dict.fromkeys(pids))


def process_mentions(pid: int, markers: tuple[str, ...]) -> bool:
    """Whether `pid`'s argv, resolved executable, or working directory mentions
    a marker (this unit's resolved repo / home paths).

    argv alone lies, both ways a service is launched: the service sessions `cd`
    into the repo and exec a RELATIVE `.venv/bin/python -m ...` (no path in
    argv), and redis overwrites its own process title seconds after starting.
    The executable and the cwd carry the marker instead — the venv python's
    resolved path, pg's `-D <home>/pg`, redis's cwd `<home>/redis`, a node
    process's cwd `<repo>/frontend`. A process none of the three can be read
    for is NOT ours (conservative — see the module docstring)."""
    import psutil

    try:
        proc = psutil.Process(pid)
    except psutil.Error:
        return False
    sources: list[list[str] | str | None] = []
    for attr in ("cmdline", "exe", "cwd"):
        try:
            sources.append(getattr(proc, attr)())
        except psutil.Error:
            continue
    for src in sources:
        if not src:
            continue
        text = " ".join(src) if isinstance(src, list) else src
        if any(marker in text for marker in markers):
            return True
    return False


def listener_is_ours(port: int, markers: tuple[str, ...]) -> bool:
    """Whether every listener on `port` is plausibly this unit's own service."""
    pids = listeners_on(port)
    if not pids:
        return False
    return all(process_mentions(pid, markers) for pid in pids)


def unit_port_map(home: Path) -> dict[str, int]:
    """The full service->port map THIS unit expects to own on this host.

    The cluster's port block (`expected_cluster_ports`: registry record, or the
    legacy block for a record-less default home) overlaid by this unit's health
    ports — the per-machine layer `ava enroll --health-port-base` moves. The
    overlay OVERRIDES the block map, not fills its gaps: for a record-less
    enrolled unit the block map is the LEGACY segment while the unit's own .env
    declares its real per-unit ports (for a record-having cluster the two agree,
    since derive_env writes both from the same base). This is the exact set the
    start preflight scans and `ava stop`'s orphan sweep reaps."""
    from shared.daemon_health import health_port
    from shared.env_registry import health_port_env_aliases

    ports: dict[str, int] = dict(cast("dict[str, int]", expected_cluster_ports(home)))
    for svc in health_port_env_aliases():
        ports[svc] = health_port(svc)
    return ports
