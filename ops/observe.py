"""Read-only service status derived from the canonical root roster.

The observer preserves each service's probe kind, readiness verdict, failure
detail and configuration gate. It neither launches services nor evaluates
release decisions; root supervision owns process recovery.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from ops.service_spec import ServiceSpec
from ops.spec import services_for_capabilities_annotated
from shared.machine import MachineRoles
from shared.proc import process_alive
from shared.resilience import ExponentialBackoff, Policy, http_classifier, retry

_log = logging.getLogger(__name__)


@dataclass(frozen=True)
class ProbeView:
    """The scheduling view for one service — how Status watches it, independent of
    whether it is currently up.

    Attributes:
        session: the service kebab.
        kind: probe signal type — ``"http"`` / ``"tcp"`` / ``"pid"`` / ``"none"``.
        target: the probe target (URL / ``str(port)`` / pidfile path / ``""``).
        healthcheck_module: the protocol healthcheck module, or None if unmonitored.
        gate_reason: None if the service is active, else why it is gated out
            (Status shows the reason rather than hiding the row).
    """

    session: str
    kind: str
    target: str
    healthcheck_module: str | None
    gate_reason: str | None


@dataclass(frozen=True)
class ServiceStatus:
    """One service's observed liveness.

    ``alive`` is True/False from the probe, or None when there is no probe to run
    (a probe-less service, or one gated out — ``gate_reason`` then says why).

    ``detail`` is the failing probe's own words, empty when there is nothing to
    add. An identity probe's verdict is the one that most needs them: "down" and
    "answering, but its home is /home/ava/.ava" are the same ``alive=False`` and
    call for completely different actions, and without this field the observation
    layer was the one surface that dropped the distinction on the floor."""

    session: str
    kind: str
    alive: bool | None
    gate_reason: str | None
    detail: str = ""


def _probe_kind_target(spec: ServiceSpec) -> tuple[str, str]:
    """Classify a service's probe signal + its target, mirroring the ``ava status``
    precedence (identity → curl → tcp → pidfile → none)."""
    if spec.identity_probe is not None:
        # The target is still the endpoint dialled; only the strength of the
        # answer differs, and that is what `kind` reports.
        return "identity", spec.curl_url or ""
    if spec.curl_url is not None:
        return "http", spec.curl_url
    if spec.tcp_port is not None:
        return "tcp", str(spec.tcp_port)
    if spec.pidfile is not None:
        return "pid", str(spec.pidfile)
    return "none", ""


def probe_set(roles: MachineRoles) -> tuple[ProbeView, ...]:
    """The scheduling/probe view for a host's capabilities — the set of services
    Status watches, each with how it is probed and its healthcheck module. Derived from
    the Spec roster, so it can never drift from what ``ava start`` launches."""
    views: list[ProbeView] = []
    for spec, gate_reason in services_for_capabilities_annotated(roles):
        kind, target = _probe_kind_target(spec)
        views.append(
            ProbeView(
                session=spec.session,
                kind=kind,
                target=target,
                healthcheck_module=spec.healthcheck_module,
                gate_reason=gate_reason,
            )
        )
    return tuple(views)


def _run_probe(spec: ServiceSpec) -> tuple[bool | None, str]:
    """Run a service's probe; ``(None, "")`` when there is no probe to run.

    Faithful to ``cli.commands._probe._probe_service``: identity, HTTP 2xx/3xx,
    TCP connect, pidfile + ``process_alive``, in that precedence — and, now, in
    what it reports. The verdict alone flattens an occupant on our port into the
    same ``False`` as a dead daemon, which is the distinction the identity probes
    exist to draw; the detail rides out with it so a consumer of the observation
    layer can act on the difference instead of re-dialling to learn it."""
    if spec.identity_probe is not None:
        probe = spec.identity_probe()
        # `alive` only — never `terminal`. Whether a respawn could win is the
        # supervisor's question; this layer reports what is observed.
        return probe.alive, ("" if probe.alive else probe.detail)
    if spec.curl_url is not None:
        ok = _curl_ok(spec.curl_url)
        return ok, ("" if ok else f"no 2xx/3xx from {spec.curl_url}")
    if spec.tcp_port is not None:
        ok = _tcp_ok(spec.tcp_port)
        return ok, ("" if ok else f"nothing accepting on port {spec.tcp_port}")
    if spec.pidfile is not None:
        try:
            pid = int(spec.pidfile.read_text().strip())
        except (FileNotFoundError, ValueError):
            return False, f"no readable pid in {spec.pidfile}"
        alive = process_alive(pid)
        return alive, ("" if alive else f"pid {pid} is not running")
    return None, ""


# Probe confirm-retry (R2-D, audit-06 Q2): a transient TCP reset / slow
# response at the probe instant must not read as down — one 1s confirm
# retry; 4xx stays immediate (a misconfigured probe); a probe must never
# sleep for the upstream's Retry-After.
_PROBE_POLICY = Policy(
    max_attempts=2,
    backoff=ExponentialBackoff(base=1.0, factor=1.0, cap=1.0),
    jitter="none",
    classify=http_classifier,
    respect_retry_after=False,
)


def _curl_ok(url: str) -> bool:
    # Probe confirm-retry (R2-D, audit-06 Q2) — same policy as
    # cli/commands/_probe.py's probe: one 1s confirm for transient
    # failures, no Retry-After respect.
    import httpx

    def _get() -> None:
        resp = httpx.get(url, timeout=5.0, follow_redirects=False)
        resp.raise_for_status()

    try:
        retry(_PROBE_POLICY)(_get)
    except httpx.HTTPError as exc:
        _log.warning("probe %s failed: %s", url, exc)
        return False
    return True


def _tcp_ok(port: int) -> bool:
    import socket

    try:
        with socket.create_connection(("127.0.0.1", port), timeout=2):
            return True
    except OSError:
        return False


def observe_services(roles: MachineRoles) -> tuple[ServiceStatus, ...]:
    """Probe every service in a host's roster and return its observed liveness.

    A gated-out service is not probed — it reports ``alive=None`` with its
    ``gate_reason`` — so a service ``ava start`` chose not to launch reads as
    "n/a: <reason>" instead of a scary ``✗``."""
    out: list[ServiceStatus] = []
    for spec, gate_reason in services_for_capabilities_annotated(roles):
        if gate_reason is not None:
            out.append(
                ServiceStatus(
                    session=spec.session, kind="gated", alive=None, gate_reason=gate_reason
                )
            )
            continue
        kind, _target = _probe_kind_target(spec)
        alive, detail = _run_probe(spec)
        out.append(
            ServiceStatus(
                session=spec.session, kind=kind, alive=alive, gate_reason=None, detail=detail
            )
        )
    return tuple(out)
