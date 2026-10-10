"""Deployment diagnostics: protocol observation and alerting.

Endpoint configuration and authentication follow their existing owners. No adapter calls
an ensure, start, stop, launchd repair, or session command.
"""

from __future__ import annotations

import shutil
from collections.abc import Callable
from pathlib import Path
from urllib.parse import urlsplit

from base.config import settings
from base.daemon.health import DaemonProbe
from base.db import Database
from base.native_process.os_platform import is_macos
from services.supervision.ava_root_glue.diagnostics import Diagnostic


def brew_pins() -> DaemonProbe:
    from base.host import brew_pin, proc

    observed: list[set[str]] = []
    for option in ("--pinned", "--formula"):
        result = proc.run_bounded(
            ["brew", "list", option], timeout=5, capture_output=True, text=True
        )
        result.check_returncode()
        observed.append(set(result.stdout.splitlines()))
    pinned, installed = observed
    missing = sorted((brew_pin.PINNED_BREW_FORMULAE & installed) - pinned)
    if missing:
        return DaemonProbe.down(f"approved Homebrew formulae are unpinned: {', '.join(missing)}")
    return DaemonProbe.up("installed approved Homebrew formulae are pinned")


def venv() -> DaemonProbe:
    from services.supervision.healthchecks import prod_venv

    if shutil.which("uv") is None:
        return DaemonProbe.unavailable("uv unavailable: dependency consistency was not measured")
    # Inspect the checkout actually executing root, never a different cluster's
    # production checkout. The helper's default remains for direct callers.
    violations = prod_venv.venv_violations(source_root=Path(__file__).resolve().parents[3])
    if violations:
        return DaemonProbe.down("; ".join(violations))
    return DaemonProbe.up("root checkout dependency and import checks passed")


def redis_acl() -> DaemonProbe:
    from base.cluster import ownership
    from services.supervision.healthchecks import protocol_probe
    from services.supervision.healthchecks import redis_acl as check

    port = ownership.configured_redis_port()
    if port is None:
        return DaemonProbe.unavailable("no explicit local Redis endpoint")

    def ping() -> DaemonProbe:
        from redis.exceptions import RedisError

        try:
            check.ping(settings.data_plane.redis_url)
        except RedisError as exc:
            return DaemonProbe.down(f"native Redis runtime ACL PING failed: {type(exc).__name__}")
        return DaemonProbe.up("Redis runtime ACL authenticated and answered PING")

    return protocol_probe.probe_protocol(ping)


def pgbouncer() -> DaemonProbe:
    from base.cluster import get_record
    from base.cluster.authority import POOLER_ADMIN, AuthorityRefusedError, read_pooler_admin
    from base.cluster.dataplane import pooler
    from base.paths import ava_home
    from services.supervision.healthchecks import protocol_probe

    record = get_record(ava_home())
    if record is None:
        return DaemonProbe.unavailable("no registry record for the local pooler")
    port = record.ports["pgbouncer"]
    try:
        admin_password = read_pooler_admin(ava_home().resolve()).password
    except AuthorityRefusedError as exc:
        return DaemonProbe.unavailable(f"no pooler admin credential: {exc}")

    def protocol() -> DaemonProbe:
        loopback = pooler.pgbouncer_listener_reachable(port, admin_password)
        public = loopback and pooler.pgbouncer_public_listener_reachable(
            port, POOLER_ADMIN, settings.data_plane.cluster_secret
        )
        if loopback and public:
            return DaemonProbe.up("native pooler admin console and required listeners answered")
        return DaemonProbe.down(f"pooler listener failure: loopback={loopback}, public={public}")

    return protocol_probe.probe_protocol(protocol)


def browser_reach() -> DaemonProbe:
    from services.desktop.browser.probe import probe_browser
    from services.supervision.healthchecks import browser_reach as check
    from services.supervision.healthchecks import protocol_probe

    url = settings.services.gateway_health_url.strip()
    if not url:
        return DaemonProbe.unavailable("gateway reachability URL is not configured")
    port = settings.services.browser_cdp_port

    def protocol() -> DaemonProbe:
        browser = probe_browser()
        if not browser.alive:
            return DaemonProbe.unavailable(f"browser canary prerequisite failed: {browser.detail}")
        canary = check.canary(port, url, settings.services.browser_reach_timeout_s)
        if canary.outcome == "skip":
            return DaemonProbe.unavailable(canary.detail)
        if canary.outcome == "ok":
            return DaemonProbe.up(canary.detail)
        host = check.host_probe(url, settings.services.browser_reach_timeout_s)
        detail = f"browser={canary.detail}; host={host.detail}"
        if not host.ok:
            return DaemonProbe.unavailable(f"host baseline also failed: {detail}")
        return DaemonProbe.down(f"browser-specific reachability failure: {detail}")

    return protocol_probe.probe_protocol(protocol)


class StationProbe:
    """Gateway-only remote protocol observation."""

    def __init__(self, *, database: Callable[[], Database]) -> None:
        from services.wake.heartbeat import station_probe

        self._module = station_probe
        self._database = database

    def probe(self) -> DaemonProbe:
        if not settings.data_plane.cluster_secret:
            return DaemonProbe.unavailable("station probe lacks its authentication credential")
        target = self._module.resolve_target(database=self._database)
        if target is None:
            return DaemonProbe.unavailable("configured station target could not be resolved")
        if self._module._station_answers(target.url):
            return DaemonProbe.up("remote station authenticated OTLP request accepted")
        return DaemonProbe.down("remote station OTLP ingress failed")


def lgtm_write_path() -> DaemonProbe:
    from base.telemetry.lgtm_local import backend_urls
    from services.supervision.healthchecks import lgtm, protocol_probe

    port = urlsplit(backend_urls()["loki"]).port
    if port is None:
        return DaemonProbe.unavailable("native Loki endpoint has no explicit port")

    def protocol() -> DaemonProbe:
        ok, reason = lgtm.write_path_probe()
        return DaemonProbe.up(reason) if ok else DaemonProbe.down(reason)

    return protocol_probe.probe_protocol(protocol)


class LokiReport:
    """Retain write-path failure events without granting a backend restart verb."""

    def __init__(self) -> None:
        self._failures = 0
        self._throttles = 0

    def __call__(self, result: DaemonProbe) -> None:
        if result.alive:
            self._failures = 0
            self._throttles = 0
            return
        from base.log import logger

        if result.detail == "push_http_429":
            self._failures = 0
            self._throttles += 1
            if self._throttles == 3 or self._throttles % 30 == 0:
                logger.warning(
                    "Loki write path remains throttled",
                    event="loki_write_path_probe_throttled",
                    consecutive_throttles=self._throttles,
                    reason=result.detail,
                )
            return
        self._throttles = 0
        self._failures += 1

        logger.warning(
            "Loki write path failed: {reason}",
            event="loki_write_path_probe_failed",
            consecutive_failures=self._failures,
            reason=result.detail,
        )


def build_diagnostics(requested: set[str], *, database: Callable[[], Database]) -> list[Diagnostic]:
    """Host policy is explicit; absent capabilities do not create fake samples."""
    from base.cluster.machine import is_gateway

    checks: list[Diagnostic] = []
    if is_macos():
        checks.append(Diagnostic("brew-pin", brew_pins))
    checks.append(Diagnostic("venv", venv))
    if is_macos():
        from services.supervision.healthchecks.permissions_helper import episode_reporter, probe

        checks.append(Diagnostic("permissions-helper", probe, report=episode_reporter()))
    if is_gateway():
        if not settings.data_plane.is_remote:
            checks.append(Diagnostic("redis-acl", redis_acl))
            if settings.data_plane.pgbouncer_enabled:
                checks.append(Diagnostic("pgbouncer", pgbouncer))
        if settings.observability.observability_url.strip():
            station = StationProbe(database=database)
            checks.append(Diagnostic("observatory-station", station.probe))
    if "browser" in requested:
        checks.append(
            Diagnostic(
                "browser-reach",
                browser_reach,
                interval_s=max(60, settings.services.browser_reach_probe_interval_s),
                timeout_s=max(20, settings.services.browser_reach_timeout_s * 3 + 10),
            )
        )
    if "loki" in requested:
        checks.append(Diagnostic("lgtm-write-path", lgtm_write_path, report=LokiReport()))
    return checks
