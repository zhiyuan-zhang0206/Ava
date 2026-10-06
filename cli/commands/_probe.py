"""Identity-bound service diagnostics and startup failure reporting.

The canonical roster supplies a protocol probe bound to root's captured process
birth. Missing or unobservable evidence is unavailable; a responding foreign
process cannot certify this cluster. Root owns startup readiness and recovery.
"""

from __future__ import annotations

import logging
import sys
from typing import Any, NamedTuple

from base import telemetry
from base.deploy.git.cluster_drift import prod_source_branch_drift as _detect_prod_source_drift
from base.deploy.progress_timeout import CRITICAL_SERVICE_SESSIONS as CRITICAL_SERVICE_SESSIONS
from base.host.net.resilience import ExponentialBackoff, Policy, http_classifier, retry
from cli.commands._repo import ServiceSpec, session_name

__all__ = ["_detect_prod_source_drift"]

logger = logging.getLogger(__name__)


# Probe confirm-retry (R2-D, audit-06 Q2): a transient TCP reset / slow
# response at the probe instant must not read as down and feed the failure signal —
# one 1s confirm retry. 4xx stays immediate
# (a misconfigured probe), 429/5xx get the confirm. No Retry-After respect:
# a probe must never sleep for the upstream's backoff.
_PROBE_POLICY = Policy(
    max_attempts=2,
    backoff=ExponentialBackoff(base=1.0, factor=1.0, cap=1.0),
    jitter="none",
    classify=http_classifier,
    respect_retry_after=False,
)


def _curl_ok(url: str) -> bool:
    # httpx (a dependency) instead of shelling out to `curl` — `curl` is not
    # guaranteed on PATH (and the old `-o /dev/null` is POSIX-only). Same intent:
    # a 2xx/3xx HTTP response means the service is up.
    import httpx

    def _get() -> None:
        resp = httpx.get(url, timeout=5.0, follow_redirects=False)
        resp.raise_for_status()

    try:
        retry(_PROBE_POLICY)(_get)
    except httpx.HTTPError as exc:
        logger.warning("probe %s failed: %s", url, exc)
        return False
    return True


class ServiceProbe(NamedTuple):
    """Operator-facing readiness evidence.

    ``alive=None`` means missing or unobservable evidence. Only ``True`` is a
    positive readiness claim. ``terminal`` identifies an occupied endpoint or
    unknown ownership that startup must resolve before launching.
    """

    alive: bool | None
    label: str
    detail: str
    terminal: bool = False


def _probe_service(spec: ServiceSpec) -> ServiceProbe:
    """Report only identity-bound protocol evidence from the canonical roster."""
    if spec.identity_probe is None:
        return ServiceProbe(None, "unavailable", "service has no identity-bound readiness probe")
    try:
        probe = spec.identity_probe()
    except Exception as exc:
        return ServiceProbe(
            None, "unavailable", f"identity probe raised {type(exc).__name__}: {exc}"
        )
    from base.daemon.health import ProbeVerdict

    alive = None if probe.verdict is ProbeVerdict.UNAVAILABLE else probe.alive
    return ServiceProbe(alive, "identity", "" if alive else probe.detail, probe.terminal)


class OccupiedPort(NamedTuple):
    """One health port this start was about to bind that someone else answers on.

    `detail` is the probe's own words, which already name the occupant's
    `$AVA_HOME` and the URL — the two facts an operator needs to decide which
    unit should move.
    """

    spec: ServiceSpec
    detail: str


def _binds_a_daemon_health_port(spec: ServiceSpec) -> bool:
    """Whether `spec`'s port answers an Ava `/healthz` whose body names its home.

    Read off what the spec declares, not off its URL: a standard daemon carries a
    `health_name` (its URL is built from the same `ServiceEndpoints` row the
    daemon passes to `start_health_server`, so the URL probed and the port bound
    cannot disagree), and the gate declares `home_healthz` for its entry port. The
    gateway (`/api/health`), the browser (CDP `/json/version`) and
    the frontend declare neither: none of them serves a `/healthz` whose body
    names its home.
    """
    return spec.health_name is not None or spec.home_healthz


def _occupied_health_ports(specs: tuple[ServiceSpec, ...]) -> tuple[OccupiedPort, ...]:
    """The health ports among `specs` that another unit already answers on.

    `ava start` probes before it binds because the alternative is worse in both
    directions. A daemon launched onto a taken port dies on `[Errno 48] Address
    already in use` and is respawned every watchdog round forever; a daemon whose
    port is being *relayed* from elsewhere is worse still, because the watchdog's
    probe is answered — by the wrong process — and the operator's `ava start`
    prints a clean roster (issue #977: 402 identity-mismatch lines over one
    afternoon, each ending "manual intervention needed").

    Only a `terminal` verdict counts, and that is the whole discrimination: this
    unit's own daemon answers with its own home+name and is ALIVE, a stray of its
    own home is DOWN, and an empty port is DOWN — so an idempotent restart, a
    crashed daemon, and a cold host all pass. `PORT_TAKEN` is reached only by
    something no respawn of ours can dislodge, which is exactly the condition
    that makes launching pointless rather than merely slow.

    Nothing is remembered: the check is a fresh dial every start, so once the
    occupant leaves, the next start proceeds with no state to clear.

    **Only an occupant that answers HTTP on `/healthz` is detectable.** The
    verdict comes from reading a health payload, so a listener speaking any other
    protocol (a Postgres or redis on the port), one that accepts and stays silent,
    and an HTTP server that 404s the path all read DOWN — indistinguishable from
    an empty port — and the start proceeds into the `EADDRINUSE` this gate exists
    to prevent. The gate narrows the failure it was built for (issue #977's relay,
    which answers) and leaves the generic taken-port case where it already was.
    """

    occupied: list[OccupiedPort] = []
    for spec in specs:
        if not _binds_a_daemon_health_port(spec):
            continue
        probe = _probe_service(spec)
        if probe.terminal:
            occupied.append(OccupiedPort(spec, probe.detail))
    return tuple(occupied)


class ReadinessWait(NamedTuple):
    """What the readiness wait found, and how it stopped looking.

    `unready` alone cannot answer the operator's first question, because the two
    exits below mean opposite things about the bound. A wait that spent 180 s on
    live-but-not-serving daemons is a case where waiting longer might have helped;
    a wait that returned in a second because every session was gone never spent the
    bound at all, and raising it would change nothing. Reporting only the specs made
    both print the same sentence, and on 2026-07-30 that sentence sent the first
    diagnosis of a failed rollout looking for a too-tight timeout on a wait that had
    taken about a second (#1016).

    `sessions_gone` is the early exit, so it is a fact about *every* spec in
    `unready`; the deadline exit implies at least one of them was still alive (the
    early exit requires all of them to be gone). Both exits concern the CRITICAL
    roster only: `non_critical_unready` carries the demoted services that missed
    the current availability snapshot — they are reported and signalled, but never
    decide the exit code.
    """

    unready: tuple[ServiceSpec, ...]
    elapsed_s: float
    sessions_gone: bool
    # Defaulted so existing constructions (tests/fixtures/guards.py's guard included)
    # keep meaning "nothing non-critical failed".
    non_critical_unready: tuple[ServiceSpec, ...] = ()


def _print_unready_services(wait: ReadinessWait, timeout_s: float) -> None:
    """Name the services that never became ready, for the operator reading the same
    output as the status snapshot above it.

    The snapshot already shows a cross on each row; this says which crosses are the
    reason for the non-zero exit, so `rc != 0` never arrives without the *what*
    beside it. Printed AFTER the snapshot deliberately — an operator reads down.

    The two exits get two different sentences because they send the reader to two
    different places. Naming the bound is only meaningful when the bound was spent:
    the services are alive and still not serving, so waiting longer might help. When
    every session is gone the wait returned in about a second, the bound is
    irrelevant, and the next question is why the process is absent — printing the
    bound there states an elapsed time the surrounding timestamps contradict, which
    is how the 2026-07-30 rollout post-mortem opened by hunting a timeout that was
    never reached (#1016)."""
    names = ", ".join(session_name(s.session) for s in wait.unready)
    if wait.sessions_gone:
        print(
            f"\n✗ {len(wait.unready)} service(s) not ready after {wait.elapsed_s:.1f}s "
            f"— their sessions are gone: {names}\n"
            f"  They died or were never launched, so the {timeout_s:.0f}s readiness bound was "
            f"not spent and raising it would change nothing;\n"
            f"  read why in $AVA_HOME/logs. ava-root keeps trying to revive them and "
            f"`ava start` is idempotent to retry.",
            file=sys.stderr,
        )
        return
    print(
        f"\n✗ {len(wait.unready)} service(s) never became ready within {timeout_s:.0f}s "
        f"({wait.elapsed_s:.1f}s elapsed): {names}\n"
        f"  Their sessions are running and still not serving — their rows above show the "
        f"failing probe. Every start step itself succeeded,\n"
        f"  so this host is up but incomplete; ava-root keeps trying to revive them, "
        f"`ava status` re-checks, and `ava start` is idempotent to retry.",
        file=sys.stderr,
    )


def _print_non_critical_unready_services(specs: tuple[ServiceSpec, ...]) -> None:
    """Report optional capabilities unavailable when core startup finishes.

    The counterpart of `_print_unready_services` for the tier that cannot fail
    the start. The cross must still appear — the tier downgrade is a verdict
    change, never a silence — and it points at the event that was emitted.
    """
    names = ", ".join(session_name(s.session) for s in specs)
    print(
        f"\n✗ {len(specs)} non-critical service(s) currently unavailable: {names}\n"
        f"  They do not fail this start (the readiness gate waits for the critical roster "
        f"only),\n"
        f"  but a service_start_unready event was emitted; ava-root keeps trying to revive them and "
        f"`ava start` is idempotent to retry.",
        file=sys.stderr,
    )


def _report_non_critical_unready_services(specs: tuple[ServiceSpec, ...]) -> None:
    """Emit one `service_start_unready` event per unavailable optional service.

    The tier's second rail: the demotion must not go silent. The boot job's
    uncapped 60 s retries repeat the event while the failure stays open; it
    stops once a start finds the service up.
    """
    for spec in specs:
        telemetry.emit(
            "telemetry",
            "service_start_unready",
            level="warning",
            source="start",
            attributes={"service": session_name(spec.session)},
        )


def _print_service_row(
    spec: ServiceSpec,
    name_w: int,
    skip_reason: str | None = None,
    *,
    root_units: dict[str, dict[str, Any]],
) -> None:
    sess = session_name(spec.session)
    unit = root_units.get(spec.session)
    session_mark = "✓" if unit is not None and unit.get("state") == "running" else "✗"

    probe = _probe_service(spec)
    if probe.alive is True:
        probe_mark = "✓"
    elif probe.alive is False:
        probe_mark = "✗"
    else:
        probe_mark = "·"

    # A gated-out service (e.g. browser) is shown WITH its reason rather than
    # hidden. The marks still reflect real liveness, so a still-running but now-
    # gated session reads as "✓ ... -- skipped: <reason>", surfacing the mismatch.
    # A failing probe's detail rides on the same line: "down" and "answering, but
    # its home is /home/ava/.ava" call for completely different actions, and the
    # operator has no other place to learn which one they are looking at.
    suffix = f"   -- skipped: {skip_reason}" if skip_reason else ""
    if not suffix and probe.detail:
        suffix = f"   -- {probe.detail}"
    print(f"{sess.ljust(name_w)}  {session_mark}     {probe_mark} ({probe.label}){suffix}")
