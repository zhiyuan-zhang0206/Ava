"""Page-server singleton resources derived from its explicit entry configuration."""

import logging
import sys
from pathlib import Path

import services.pidfile as pidfiles
from base.config import ConfigBoot
from base.daemon.endpoints import ServiceEndpoint, ServiceEndpoints

__all__ = ["endpoint", "is_running", "pidfile", "remove_pidfile", "write_pidfile"]

_log = logging.getLogger("services.agent_runner.page_server.daemon")


def endpoint(config: ConfigBoot, *, runtime: Path) -> ServiceEndpoint:
    runtime.mkdir(parents=True, exist_ok=True)
    return ServiceEndpoints.from_boot(config, runtime=runtime).of("page_server")


def pidfile(config: ConfigBoot, *, runtime: Path) -> Path:
    return endpoint(config, runtime=runtime).pidfile


def write_pidfile(config: ConfigBoot, *, runtime: Path) -> None:
    if not pidfiles.acquire_pidfile(
        pidfile(config, runtime=runtime), "services.agent_runner.page_server.daemon"
    ):
        _log.info(
            "[page-server] daemon already running (pidfile=%s), exiting",
            pidfile(config, runtime=runtime),
        )
        sys.exit(1)


def remove_pidfile(config: ConfigBoot, *, runtime: Path) -> None:
    pidfiles.remove_pidfile(pidfile(config, runtime=runtime))


def is_running(config: ConfigBoot, *, runtime: Path) -> bool:
    """Whether a daemon is already running (via its pidfile)."""
    return pidfiles.pidfile_holds_daemon(
        pidfile(config, runtime=runtime), "services.agent_runner.page_server.daemon"
    )
