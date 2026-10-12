"""Retire the legacy launchd Redis bridge and observe the root-supervised relay.

Redis remains bound to loopback.  On a split gateway the `redis-bridge` roster unit
(`services/redis_bridge/relay.py`, supervised by the root like every other service)
accepts the cluster's authenticated private-network traffic and forwards it to the same
port on ``127.0.0.1``.  Converge only removes the standalone launchd job and installed
copy an earlier layout used, so exactly one supervisor owns the listener.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import NamedTuple

from base.cluster.dataplane.redis_bridge import (
    RedisBridgeUnconfiguredError,
    redis_bridge_endpoint,
)
from base.cluster.machine import MachineIdentity
from base.config import ConfigBoot
from base.host.net.url_secret import url_with_host
from cli.commands.converge.spec import ConvergeCtx

logger = logging.getLogger("cli.converge.redis_bridge")

_LABEL = "com.ava.redis-bridge"
_BOOTOUT_TIMEOUT_S = 10.0
_BOOTOUT_POLL_INTERVAL_S = 0.25

_sleep = time.sleep


class RedisBridgeStatus(NamedTuple):
    required: bool
    endpoint: str
    serving: bool
    detail: str


def _installed_path(home: Path) -> Path:
    return home / "redis-bridge" / "relay.py"


def _plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{_LABEL}.plist"


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


def _retire_launchd_bridge(home: Path) -> None:
    """Unload and remove the standalone launchd job and installed copy."""
    plist_path = _plist_path()
    installed_path = _installed_path(home)
    loaded = _job_loaded()
    if not loaded and not plist_path.exists() and not installed_path.exists():
        return
    if loaded and not _bootout_and_wait():
        raise RuntimeError(
            f"Redis bridge LaunchAgent {_LABEL} remained loaded after "
            f"{_BOOTOUT_TIMEOUT_S:.0f}s; retained {plist_path} for inspection"
        )
    plist_path.unlink(missing_ok=True)
    installed_path.unlink(missing_ok=True)
    logger.info("Retired the standalone Redis bridge LaunchAgent and installed source")


def retire_launchd_redis_bridge(ctx: ConvergeCtx) -> None:
    """Leave the relay to the root supervisor: no standalone launchd job may hold its port."""
    if sys.platform == "darwin":
        _retire_launchd_bridge(ctx.ava_home)


def probe_redis_bridge(
    home: Path | None = None, *, machine: MachineIdentity, config: ConfigBoot
) -> RedisBridgeStatus:
    """PING Redis through the relay's private-network endpoint."""
    from base.paths import ava_home

    target = (home or ava_home()).expanduser()
    try:
        bridge = redis_bridge_endpoint(
            target,
            machine_host=machine.host,
            remote_data_plane=lambda: config.view.data_plane.is_remote,
        )
    except RedisBridgeUnconfiguredError as exc:
        return RedisBridgeStatus(required=True, endpoint="", serving=False, detail=str(exc))
    if bridge is None:
        return RedisBridgeStatus(required=False, endpoint="", serving=True, detail="not required")

    endpoint = f"{bridge.listen_host}:{bridge.port}"
    try:
        import redis

        bridge_url = url_with_host(config.view.data_plane.redis_url, bridge.listen_host)
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
        return RedisBridgeStatus(required=True, endpoint=endpoint, serving=False, detail=detail)
    return RedisBridgeStatus(
        required=True, endpoint=endpoint, serving=True, detail="Redis PING succeeded"
    )


def print_redis_bridge_status(*, machine: MachineIdentity, config: ConfigBoot) -> None:
    status = probe_redis_bridge(machine=machine, config=config)
    if not status.required:
        print("  · not required (Redis has no local off-box ingress)")
        return
    mark = "✓" if status.serving else "✗"
    endpoint = f" {status.endpoint}" if status.endpoint else ""
    print(f"  {mark}{endpoint} Redis PING: {status.detail}")
