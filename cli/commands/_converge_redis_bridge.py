"""Converge and observe the macOS Redis private-network bridge.

Redis remains bound to loopback.  On a split gateway, this host-level launchd
job accepts the cluster's authenticated private-network traffic and forwards it
to the same port on ``127.0.0.1``.  Converge copies the pure-stdlib relay into a
stable path under ``$AVA_HOME`` and owns the launchd plist that executes it.
"""

from __future__ import annotations

import logging
import os
import plistlib
import stat
import subprocess
import sys
import time
from pathlib import Path
from typing import NamedTuple

from cli.commands._converge_spec import ConvergeCtx
from shared.config import settings
from shared.netutil import is_loopback_host
from shared.url_secret import url_with_host

logger = logging.getLogger("cli.converge.redis_bridge")

_LABEL = "com.ava.redis-bridge"
_SYSTEM_PYTHON = "/usr/bin/python3"
_BOOTOUT_TIMEOUT_S = 10.0
_BOOTOUT_POLL_INTERVAL_S = 0.25
_BOOTSTRAP_BACKOFF_S = (1.0, 3.0)

_sleep = time.sleep


class RedisBridgeConfig(NamedTuple):
    listen_host: str
    port: int


class RedisBridgeStatus(NamedTuple):
    required: bool
    endpoint: str
    serving: bool
    supervised: bool
    detail: str


def _bridge_config(home: Path) -> RedisBridgeConfig | None:
    """Return this host's relay config, or None when no relay should exist."""
    if sys.platform != "darwin" or settings.data_plane.is_remote:
        return None
    if not settings.data_plane.cluster_secret:
        return None

    from shared.cluster import get_record, record_redis_port
    from shared.machine import reachable_host

    listen_host = reachable_host()
    if is_loopback_host(listen_host):
        return None
    record = get_record(home)
    if record is None:
        raise RuntimeError(f"no registry record for {home} — cannot configure Redis bridge")
    return RedisBridgeConfig(listen_host=listen_host, port=record_redis_port(record))


def _source_path(repo: Path) -> Path:
    return repo / "services" / "redis_bridge" / "relay.py"


def _installed_path(home: Path) -> Path:
    return home / "redis-bridge" / "relay.py"


def _plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{_LABEL}.plist"


def _install_source(source: Path, destination: Path) -> bool:
    """Atomically install ``source``; return whether its bytes changed."""
    content = source.read_bytes()
    if destination.exists() and destination.read_bytes() == content:
        if stat.S_IMODE(destination.stat().st_mode) == 0o755:
            return False
        destination.chmod(0o755)
        return True
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.{os.getpid()}.tmp")
    try:
        temporary.write_bytes(content)
        temporary.chmod(0o755)
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)
    return True


def _plist_content(home: Path, config: RedisBridgeConfig) -> bytes:
    script = _installed_path(home)
    log_path = home / "redis-bridge" / "relay.log"
    return plistlib.dumps(
        {
            "KeepAlive": True,
            "Label": _LABEL,
            "ProgramArguments": [
                _SYSTEM_PYTHON,
                str(script),
                "--listen-host",
                config.listen_host,
                "--listen-port",
                str(config.port),
                "--backend-port",
                str(config.port),
            ],
            "RunAtLoad": True,
            "StandardErrorPath": str(log_path),
            "StandardOutPath": str(log_path),
            "ThrottleInterval": 10,
        },
        sort_keys=True,
    )


def _launchctl(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # fixed internal launchctl argv
        ["launchctl", *args],
        capture_output=True,
        text=True,
        check=False,
    )


def _job_loaded() -> bool:
    return _launchctl("print", f"gui/{os.getuid()}/{_LABEL}").returncode == 0


def _bootout_and_wait() -> bool:
    _launchctl("bootout", f"gui/{os.getuid()}/{_LABEL}")
    deadline = time.monotonic() + _BOOTOUT_TIMEOUT_S
    while _job_loaded():
        if time.monotonic() >= deadline:
            return False
        _sleep(_BOOTOUT_POLL_INTERVAL_S)
    return True


def _ensure_launchd(home: Path, repo: Path, config: RedisBridgeConfig) -> None:
    from shared.os_cron import os_jobs_enabled, skip_os_job

    if not os_jobs_enabled():
        skip_os_job("Redis bridge LaunchAgent")
        return

    script_changed = _install_source(_source_path(repo), _installed_path(home))
    plist_path = _plist_path()
    desired = _plist_content(home, config)
    if (
        not script_changed
        and plist_path.exists()
        and plist_path.read_bytes() == desired
        and _job_loaded()
    ):
        logger.info("Redis bridge LaunchAgent already current")
        return

    plist_path.parent.mkdir(parents=True, exist_ok=True)
    plist_path.write_bytes(desired)
    attempts = len(_BOOTSTRAP_BACKOFF_S) + 1
    last_failure = ""
    for attempt in range(1, attempts + 1):
        if not _bootout_and_wait():
            logger.warning(
                "Redis bridge LaunchAgent still loaded %.0fs after bootout",
                _BOOTOUT_TIMEOUT_S,
            )
        result = _launchctl("bootstrap", f"gui/{os.getuid()}", str(plist_path))
        if result.returncode == 0:
            logger.info("Redis bridge LaunchAgent loaded (KeepAlive)")
            return
        last_failure = f"rc={result.returncode}: {result.stderr.strip()}"
        logger.warning(
            "launchctl bootstrap failed for Redis bridge (attempt %d/%d, %s)",
            attempt,
            attempts,
            last_failure,
        )
        if attempt < attempts:
            _sleep(_BOOTSTRAP_BACKOFF_S[attempt - 1])
    raise RuntimeError(
        f"Redis bridge LaunchAgent would not load after {attempts} attempts "
        f"({last_failure}). Inspect `launchctl print gui/{os.getuid()}/{_LABEL}` "
        f"and {plist_path}."
    )


def ensure_redis_bridge(ctx: ConvergeCtx) -> None:
    """Install and supervise the bridge when this gateway has off-box Redis clients."""
    config = _bridge_config(ctx.ava_home)
    if config is None:
        return
    _ensure_launchd(ctx.ava_home, ctx.repo, config)


def probe_redis_bridge(home: Path | None = None) -> RedisBridgeStatus:
    """PING Redis through the relay and report its independent supervisor state."""
    from shared.paths import ava_home

    target = (home or ava_home()).expanduser()
    config = _bridge_config(target)
    if config is None:
        return RedisBridgeStatus(
            required=False,
            endpoint="",
            serving=True,
            supervised=True,
            detail="not required",
        )

    endpoint = f"{config.listen_host}:{config.port}"
    supervised = _job_loaded()
    try:
        import redis

        bridge_url = url_with_host(settings.data_plane.redis_url, config.listen_host)
        client = redis.Redis.from_url(  # pyright: ignore[reportUnknownMemberType]
            bridge_url,
            socket_connect_timeout=3.0,
            socket_timeout=3.0,
        )
        try:
            client.ping()  # pyright: ignore[reportUnknownMemberType]
        finally:
            client.close()
    except Exception as exc:
        lines = [line.strip() for line in str(exc).splitlines() if line.strip()]
        detail = lines[-1] if lines else type(exc).__name__
        return RedisBridgeStatus(
            required=True,
            endpoint=endpoint,
            serving=False,
            supervised=supervised,
            detail=detail,
        )
    return RedisBridgeStatus(
        required=True,
        endpoint=endpoint,
        serving=True,
        supervised=supervised,
        detail="Redis PING succeeded",
    )


def print_redis_bridge_status() -> None:
    status = probe_redis_bridge()
    if not status.required:
        print("  · not required (Redis has no local off-box ingress)")
        return
    mark = "✓" if status.serving else "✗"
    print(f"  {mark} {status.endpoint} Redis PING: {status.detail}")
    if status.supervised:
        print(f"  ✓ supervised by launchd job {_LABEL}")
    else:
        print(f"  ✗ launchd job {_LABEL} is absent — nothing will restart the relay")
