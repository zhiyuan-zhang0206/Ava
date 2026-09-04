"""Watchdog liveness probe for the launchd-owned macOS permissions helper."""

from __future__ import annotations

import json
import logging

from services.permissions_helper import client
from services.permissions_helper.lifecycle import repair_unresponsive_helper
from shared.log import init_gateway_process
from shared.paths import permissions_helper_socket
from shared.platform import IS_MACOS

_log = logging.getLogger("services.healthchecks.permissions_helper")

_PING_TIMEOUT_S = 3.0
_PING_RESPONSE_LIMIT = 64 * 1024
_REPAIR_AFTER_FAILURES = 3

# The watchdog imports this module once and calls main() every 60 seconds, so
# episode state stays in memory for the life of that long-running process.
_consecutive_failures: int = 0
_reported_unhealthy: bool = False
_repair_attempted: bool = False


def _ping() -> bool:
    """Ping the real helper protocol with a watchdog-sized response timeout."""
    sock = client._connect(str(permissions_helper_socket()))
    try:
        sock.settimeout(_PING_TIMEOUT_S)
        sock.sendall((json.dumps({"id": 0, "method": "ping"}) + "\n").encode())
        reply = bytearray()
        while not reply.endswith(b"\n"):
            chunk = sock.recv(4096)
            if not chunk:
                break
            reply.extend(chunk)
            if len(reply) > _PING_RESPONSE_LIMIT:
                raise client.PermissionsHelperError(
                    "permissions helper ping response exceeded line limit"
                )
    finally:
        sock.close()

    result: client.PingResult = client._parse_reply(bytes(reply), "ping")
    return result["pong"] is True


def _clear_unhealthy_episode() -> None:
    global _consecutive_failures, _repair_attempted, _reported_unhealthy  # noqa: PLW0603

    _consecutive_failures = 0
    _reported_unhealthy = False
    _repair_attempted = False


def main() -> None:
    """Report helper failure once and repair one persistent failure episode."""
    global _consecutive_failures, _repair_attempted, _reported_unhealthy  # noqa: PLW0603

    init_gateway_process(name="permissions_helper-healthcheck")
    if not IS_MACOS:
        return

    try:
        healthy = _ping()
        failure = "pong was not true"
    except Exception as exc:
        healthy = False
        failure = f"{type(exc).__name__}: {exc}"

    if healthy:
        _clear_unhealthy_episode()
        return

    _consecutive_failures += 1
    if not _reported_unhealthy:
        _reported_unhealthy = True
        _log.error(
            "[permissions-helper healthcheck] helper did not answer ping: %s",
            failure,
        )

    if _consecutive_failures < _REPAIR_AFTER_FAILURES or _repair_attempted:
        return

    _repair_attempted = True
    try:
        repaired = repair_unresponsive_helper()
    except Exception:
        _log.warning(
            "[permissions-helper healthcheck] launchd repair failed; no further repair "
            "will run until a healthy round resets the episode",
            exc_info=True,
        )
        return

    if repaired:
        _log.warning(
            "[permissions-helper healthcheck] launchd repair answered ping; "
            "the next watchdog round will verify recovery"
        )
    else:
        _log.warning(
            "[permissions-helper healthcheck] launchd repair did not restore ping; "
            "no further repair will run until a healthy round resets the episode"
        )


if __name__ == "__main__":
    main()
