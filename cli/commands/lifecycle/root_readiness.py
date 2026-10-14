"""Fresh protocol readiness for the selected root-owned service roster."""

from __future__ import annotations

from typing import Any

from base.clock import Timers
from base.log import logger
from cli.commands.lifecycle import root_driver
from cli.commands.probe import ReadinessWait
from services.supervision.roster.service_spec import ServiceSpec


def wait_for_service_tree(
    roster: tuple[ServiceSpec, ...],
    *,
    timeout_s: float,
    timers: Timers,
) -> ReadinessWait:
    """Verify the same root-owned roster after launch."""
    return wait_for_root_services_ready(roster, timeout_s=timeout_s, timers=timers)


def health_verdicts(
    specs: tuple[ServiceSpec, ...], status: dict[str, Any] | None
) -> dict[str, str]:
    """Probe now instead of trusting a cached health result.

    A probe that raises is logged and reads as "unavailable"."""
    if status is None:
        return {}
    verdicts: dict[str, str] = {}
    for spec in specs:
        if spec.identity_probe is None:
            continue
        try:
            verdicts[spec.session] = spec.identity_probe().verdict.value
        except Exception:
            logger.opt(exception=True).warning(
                "identity probe for {} raised; treating it as unavailable", spec.session
            )
            verdicts[spec.session] = "unavailable"
    return verdicts


def unit_ready(unit: dict[str, Any] | None, verdict: str | None) -> bool:
    """Require a running unit and a positive protocol response."""
    if unit is None:
        return False
    if unit.get("state") != "running":
        return False
    return verdict == "alive"


def _unit_gone(unit: dict[str, Any] | None) -> bool:
    """A stopped unit cannot become ready without explicit reconciliation."""
    return unit is not None and unit.get("state") == "stopped"


def _fresh_readiness_round(
    client: Any, specs: tuple[ServiceSpec, ...]
) -> tuple[dict[str, dict[str, Any]], dict[str, str]]:
    """Observe the service table and ask the current protocol probes."""
    status = root_driver.root_status(client)
    units = root_driver.root_units(status) if status is not None else {}
    verdicts = health_verdicts(specs, status)
    return units, verdicts


def _unready_services(
    specs: tuple[ServiceSpec, ...],
    units: dict[str, dict[str, Any]],
    verdicts: dict[str, str],
) -> tuple[ServiceSpec, ...]:
    return tuple(
        spec
        for spec in specs
        if not unit_ready(units.get(spec.session), verdicts.get(spec.session))
    )


def wait_for_root_services_ready(
    specs: tuple[ServiceSpec, ...], timeout_s: float, *, timers: Timers
) -> ReadinessWait:
    """Wait for core services; optional availability is a diagnostic snapshot."""
    from base.deploy.progress_timeout import CRITICAL_SERVICE_SESSIONS

    client = root_driver.root_client()
    started_at = timers.monotonic()
    deadline = started_at + timeout_s
    critical = tuple(s for s in specs if s.session in CRITICAL_SERVICE_SESSIONS)
    non_critical = tuple(s for s in specs if s.session not in CRITICAL_SERVICE_SESSIONS)
    while True:
        units, verdicts = _fresh_readiness_round(client, specs)
        unready = _unready_services(critical, units, verdicts)
        non_critical_unready = _unready_services(non_critical, units, verdicts)
        now = timers.monotonic()
        gone_all = bool(unready) and all(_unit_gone(units.get(s.session)) for s in unready)
        if not unready or gone_all or now >= deadline:
            return ReadinessWait(
                unready,
                now - started_at,
                sessions_gone=gone_all,
                non_critical_unready=non_critical_unready,
            )
        timers.sleep(root_driver.READY_POLL_INTERVAL_S)
