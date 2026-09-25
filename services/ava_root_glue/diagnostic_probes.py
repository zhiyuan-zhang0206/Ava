"""Deployment diagnostics: explicit native custody, observation, then alerting.

Native data-plane resources outlive app-root maintenance and retain their own
identity readers. App endpoints require root-captured lineage. No adapter calls
an ensure, start, stop, launchd repair, or session command.
"""

from __future__ import annotations

import asyncio
import shutil
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

from services.ava_root_glue.diagnostics import Diagnostic
from shared.config import settings
from shared.daemon_health import DaemonProbe
from shared.platform import IS_MACOS, IS_WINDOWS
from shared.proc_tree import OwnedProcess
from shared.station_endpoint import StationTarget


def brew_pins() -> DaemonProbe:
    from shared import brew_pin, proc

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
    from services.healthchecks import prod_venv

    if shutil.which("uv") is None:
        return DaemonProbe.unavailable("uv unavailable: dependency consistency was not measured")
    # Inspect the checkout actually executing root, never a different cluster's
    # production checkout. The helper's default remains for direct callers.
    violations = prod_venv._violations(source_root=Path(__file__).resolve().parents[2])
    if violations:
        return DaemonProbe.down("; ".join(violations))
    return DaemonProbe.up("root checkout dependency and import checks passed")


def redis_acl() -> DaemonProbe:
    from cli.commands import _cluster_instance as instance
    from cli.commands import _maintenance_data_plane as native
    from services.healthchecks import owned_service
    from services.healthchecks import redis_acl as check

    endpoint = instance._redis_endpoint()
    if endpoint is None:
        return DaemonProbe.unavailable("no explicit local Redis endpoint")
    port, _ = endpoint

    async def capture() -> OwnedProcess:
        from redis.asyncio import Redis
        from redis.asyncio.retry import Retry
        from redis.backoff import NoBackoff

        async with Redis(
            host="127.0.0.1",
            port=port,
            password=settings.data_plane.redis_admin_password or None,
            decode_responses=True,
            single_connection_client=True,
            socket_connect_timeout=3,
            socket_timeout=3,
            retry=Retry(NoBackoff(), 0),
        ) as client:
            return await native._capture_redis(client, native.deadline_after(5))

    owner = asyncio.run(capture())

    def ping() -> DaemonProbe:
        from redis.exceptions import RedisError

        try:
            check._ping(settings.data_plane.redis_url)
        except RedisError as exc:
            return DaemonProbe.down(f"native Redis runtime ACL PING failed: {type(exc).__name__}")
        return DaemonProbe.up("native Redis runtime identity authenticated and answered PING")

    return owned_service._owned_tcp(owner, port, ping)


def pgbouncer() -> DaemonProbe:
    from cli.commands import _maintenance_data_plane as native
    from cli.commands import _pgbouncer as pooler
    from services.healthchecks import owned_service
    from shared.cluster import db_identity, get_record, record_pgbouncer_port
    from shared.paths import ava_home

    record = get_record(ava_home())
    if record is None:
        return DaemonProbe.unavailable("no registry record for the local pooler")
    owner = native._capture_pooler()
    if owner is None:
        return DaemonProbe.down("no native PgBouncer generation in this home's PID record")
    port = record_pgbouncer_port(record)
    identity = db_identity()

    def protocol() -> DaemonProbe:
        loopback = pooler.pgbouncer_listener_reachable(
            port, identity, settings.data_plane.db_admin_password
        )
        public = loopback and pooler.pgbouncer_public_listener_reachable(
            port, identity, settings.data_plane.cluster_secret
        )
        if loopback and public:
            return DaemonProbe.up("native pooler admin console and required listeners answered")
        return DaemonProbe.down(f"pooler listener failure: loopback={loopback}, public={public}")

    return owned_service._owned_tcp(owner, port, protocol)


def browser_reach() -> DaemonProbe:
    from services.browser.probe import probe_browser
    from services.healthchecks import browser_reach as check
    from services.healthchecks import owned_service

    url = settings.services.gateway_health_url.strip()
    if not url:
        return DaemonProbe.unavailable("gateway reachability URL is not configured")
    port = settings.services.browser_cdp_port

    def protocol() -> DaemonProbe:
        browser = probe_browser()
        if not browser.alive:
            return DaemonProbe.unavailable(f"browser canary prerequisite failed: {browser.detail}")
        canary = check._canary(port, url, settings.services.browser_reach_timeout_s)
        if canary.outcome == "skip":
            return DaemonProbe.unavailable(canary.detail)
        if canary.outcome == "ok":
            return DaemonProbe.up(canary.detail)
        host = check._host_probe(url, settings.services.browser_reach_timeout_s)
        detail = f"browser={canary.detail}; host={host.detail}"
        if not host.ok:
            return DaemonProbe.unavailable(f"host baseline also failed: {detail}")
        return DaemonProbe.down(f"browser-specific reachability failure: {detail}")

    return owned_service.probe_endpoint("browser", port, protocol)


class StationProbe:
    """Gateway-only remote protocol observation and its existing alert reporter."""

    def __init__(self) -> None:
        from services.heartbeat import station_probe

        self._module = station_probe
        self._target: StationTarget | None = None

    def probe(self) -> DaemonProbe:
        if not settings.data_plane.cluster_secret:
            return DaemonProbe.unavailable("station probe lacks its authentication credential")
        target = self._module.resolve_target()
        if target is None:
            return DaemonProbe.unavailable("configured station target could not be resolved")
        self._target = target
        if self._module._station_answers(target.url):
            return DaemonProbe.up("remote station authenticated OTLP request accepted")
        return DaemonProbe.down("remote station OTLP ingress failed")

    def report(self, result: DaemonProbe) -> None:
        if self._target is None:
            raise RuntimeError("station report has no observed target")
        self._module._alert_edges(self._target, ok=result.alive, now=datetime.now(UTC))


def lgtm_write_path() -> DaemonProbe:
    from services.healthchecks import lgtm, owned_service
    from shared.lgtm_local import backend_urls

    port = urlsplit(backend_urls()["loki"]).port
    if port is None:
        return DaemonProbe.unavailable("native Loki endpoint has no explicit port")

    def protocol() -> DaemonProbe:
        ok, reason = lgtm.write_path_probe()
        return DaemonProbe.up(reason) if ok else DaemonProbe.down(reason)

    return owned_service.probe_endpoint("loki", port, protocol)


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
        from shared.log import logger

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


def helper_report(result: DaemonProbe) -> None:
    from services.healthchecks import permissions_helper

    permissions_helper.report(result)


def build_diagnostics(requested: set[str]) -> list[Diagnostic]:
    """Host policy is explicit; absent capabilities do not create fake samples."""
    from shared.machine import is_gateway

    checks: list[Diagnostic] = []
    if IS_MACOS:
        checks.append(Diagnostic("brew-pin", brew_pins))
    if not IS_WINDOWS:
        checks.append(Diagnostic("venv", venv))
    if IS_MACOS:
        from services.healthchecks.permissions_helper import probe

        checks.append(Diagnostic("permissions-helper", probe, report=helper_report))
    if is_gateway():
        if not settings.data_plane.is_remote:
            checks.append(Diagnostic("redis-acl", redis_acl))
            if settings.data_plane.pgbouncer_enabled:
                checks.append(Diagnostic("pgbouncer", pgbouncer))
        if settings.observability.observability_url.strip():
            station = StationProbe()
            checks.append(Diagnostic("observatory-station", station.probe, report=station.report))
    if "browser" in requested:
        checks.append(
            Diagnostic(
                "browser-reach",
                browser_reach,
                interval_s=max(60, settings.services.browser_reach_probe_interval_s),
                timeout_s=max(20, settings.services.browser_reach_timeout_s * 3 + 10),
                failure_threshold=settings.services.browser_reach_failure_threshold,
            )
        )
    if "loki" in requested:
        checks.append(Diagnostic("lgtm-write-path", lgtm_write_path, report=LokiReport()))
    return checks
