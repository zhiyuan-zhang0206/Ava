"""Service desired-state contracts and process-profile derivation."""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, get_args

from base.cluster.machine import MachineRole
from base.daemon.health import DEFAULT_PORTS, DaemonProbe

# The capability groups a service can belong to. These are `MachineRole` values
# (`machine_role()` returns a frozenset of them); "capability" and "role" are the
# same axis in Ava's vocabulary (a unit carries a SET of capabilities).
_GATEWAY: frozenset[MachineRole] = frozenset({"gateway"})
_AGENT_RUNNER: frozenset[MachineRole] = frozenset({"agent-runner"})
_BOTH: frozenset[MachineRole] = frozenset({"gateway", "agent-runner"})

DbAccess = Literal["gateway", "runner"]


@dataclass(frozen=True)
class ServiceSpec:
    """Desired state for a single service.

    Attributes:
        session: bare service kebab (e.g. ``gateway``, ``frontend``); the real
            session name is composed by ``base.cluster.session_name``.
        cmd: shell command run in the session (wrapped in ``cd <repo> && ...``).
        capabilities: which machine capabilities run this service. A host runs the
            service iff its role set intersects this — so a gateway-only host runs
            the ``{"gateway"}`` services, an agent-runner-only host runs the
            ``{"agent-runner"}`` services, and a single box (both roles) runs the
            union. This is the single readable place that says "which machine runs
            this", replacing the old exclusion-set encoding.
        requires_db: whether this service reads or writes the cluster's Postgres.
            Required because database-scoped blocks must hold dependent services
            while leaving independent services available for diagnosis.
        db_access: the write-generation login class the launcher delivers
            (``gateway`` or ``runner``). None = derived by ``db_access`` for a
            ``requires_db`` service from its profile / capabilities; a service
            carrying both capabilities must declare it. A service that only
            sometimes dials the database (a selectable backend) declares it
            without ``requires_db``, so a database outage does not hold it.
        pidfile: pidfile path (None = no daemon-specific pidfile).
        healthcheck_module: where the service's protocol probe code lives, for
            documentation only. Root monitors a service through ``identity_probe``
            (a unit without one is refused at root wiring); this field opts nothing
            in and no code reads it to find a probe. A standard ``/healthz``
            daemon (``health_name`` set) has no module at all.
        config_inputs: authoritative external files read at process birth.
            Their paths and bytes are part of the immutable launch generation.
        stop_ceiling_s: the longest this service's own SIGTERM cleanup may run,
            when that can exceed root's default TERM window. Derive it from the
            same constants or settings the service's shutdown code reads, never
            restate the number. Root's window for the unit is this plus
            ``services.ava_root_glue.manifests.STOP_MARGIN_S``; None = the service
            finishes inside root's default window.
        curl_url: HTTP readiness endpoint; None for non-HTTP protocols.
        tcp_port: listener port for a non-HTTP protocol readiness probe.
        identity_probe: a protocol readiness callback returning DaemonProbe.
            The canonical roster binds network callbacks to root's captured
            process identity. Unix probes validate their connected peer directly.
            A response from an unrelated generation can never certify readiness.
        gate: optional predicate returning a gate reason (a string = gated OUT of
            the start roster + why, None = will start). When set it OVERRIDES the
            built-in ``_gate_reason`` lookup, so a plugin service carries its own
            domain gate instead of adding a central branch; core services still
            flow through ``_gate_reason``.
        profile: explicit ``AVA_PROCESS_PROFILE`` override for this service's
            session (default None = derived, see below). Wins over the
            derivation AND over ``no_profile_marker``. The agent-host uses it:
            it is an agent-runner-capability service whose daemon runs the
            agent kernel in-process, so its consumption matches the ``agent``
            profile, not ``runner`` (settings.agent read crashes a runner
            profile at import — 2026-08-30 soak startup).
        no_profile_marker: True = the launcher sets NO ``AVA_PROCESS_PROFILE``
            for this service's session, so the process boots profile-less (full
            Settings construction, no env-authority pop). Default False = the
            marker is derived from ``capabilities`` (gateway-only -> "gateway",
            agent-runner-only -> "runner"; both/neither -> no marker). Opt out
            for a gateway-side service whose code consumes agent-runner
            capability keys (the LLM provider keys, DEEPSEEK_API_KEY among
            them): the gateway profile's env-authority pass drops those from
            os.environ at boot, so ``settings.lm.*_api_key`` resolve to None and
            every model build fails (labeler, issue #1128 / task #1230). The
            root launcher uses this declaration on every service start.
        health_name: set only by ``ops.roster.healthz.healthz_daemon``: this
            service is a standard Ava ``/healthz`` daemon whose port is the
            ``base.daemon.health.DEFAULT_PORTS[health_name]`` slot (the same name
            the daemon passes to ``start_health_server``). None = any other shape.

    Construction validates what the declaration alone can decide, so a malformed
    spec fails where it is written instead of at ``ava start`` or root wiring:
    non-empty known capabilities, a derivable database login class, one readiness
    endpoint (``curl_url`` xor ``tcp_port``), a positive finite ``stop_ceiling_s``,
    and, for a ``health_name``, an existing port slot and a ``/healthz`` URL.
    """

    session: str
    cmd: str
    capabilities: frozenset[MachineRole]
    requires_db: bool
    pidfile: Path | None = None
    healthcheck_module: str | None = None
    curl_url: str | None = None
    tcp_port: int | None = None
    gate: Callable[[], str | None] | None = None
    identity_probe: Callable[[], DaemonProbe] | None = None
    profile: str | None = None
    no_profile_marker: bool = False
    config_inputs: tuple[Path, ...] = ()
    db_access: DbAccess | None = None
    stop_ceiling_s: float | None = None
    health_name: str | None = None

    def __post_init__(self) -> None:
        if not self.capabilities:
            raise ValueError(f"service {self.session!r} declares no capabilities")
        unknown = self.capabilities - set(get_args(MachineRole))
        if unknown:
            raise ValueError(
                f"service {self.session!r} declares unknown capabilities {sorted(unknown)}; "
                f"known: {sorted(get_args(MachineRole))}"
            )
        if self.curl_url is not None and self.tcp_port is not None:
            raise ValueError(
                f"service {self.session!r} declares both curl_url and tcp_port; "
                "its readiness endpoint is exactly one"
            )
        if self.tcp_port is not None and not 0 < self.tcp_port < 65536:
            raise ValueError(f"service {self.session!r} has invalid tcp_port {self.tcp_port}")
        ceiling = self.stop_ceiling_s
        if ceiling is not None and (not math.isfinite(ceiling) or ceiling <= 0):
            raise ValueError(
                f"service {self.session!r} stop_ceiling_s must be a positive finite number, "
                f"got {ceiling!r}"
            )
        if self.health_name is not None:
            if self.health_name not in DEFAULT_PORTS:
                raise ValueError(
                    f"service {self.session!r} names health slot {self.health_name!r}, "
                    f"which has no port (known: {sorted(DEFAULT_PORTS)})"
                )
            if self.curl_url is None or not self.curl_url.endswith("/healthz"):
                raise ValueError(
                    f"service {self.session!r} serves /healthz (health_name "
                    f"{self.health_name!r}) but its curl_url is {self.curl_url!r}"
                )
        db_access(self)  # raises for a database service whose login class cannot be decided


def profile_marker(spec: ServiceSpec) -> str | None:
    """The ``AVA_PROCESS_PROFILE`` value a launcher sets for ``spec``'s session.

    Derived from the spec's capabilities — gateway-only services run as
    ``gateway`` processes, agent-runner-only as ``runner``, and a service with
    both (or neither) gets no marker (neutral: the env-authority pop is
    skipped). ``spec.no_profile_marker`` overrides the derivation to None: the
    process boots profile-less, so the gateway profile's agent-runner-key pop
    never runs and ``settings.lm.*_api_key`` resolve from the unit's own .env.

    Returns:
        The marker value, or None when the launcher must set no marker.
    """
    if spec.profile is not None:
        return spec.profile
    if spec.no_profile_marker:
        return None
    if "gateway" in spec.capabilities and "agent-runner" not in spec.capabilities:
        return "gateway"
    if "agent-runner" in spec.capabilities and "gateway" not in spec.capabilities:
        return "runner"
    return None


def db_access(spec: ServiceSpec) -> DbAccess | None:
    """The write-generation login class the launcher delivers to ``spec``.

    The explicit declaration first; else None for a service that does not use
    the database; else the profile (``gateway`` -> gateway; ``runner`` / ``agent``
    -> runner), else the single capability of a profile-less service. A
    database service carrying both capabilities has no derivable class and must
    declare one: there is no fallback to an owner or administrator credential.

    Raises:
        ValueError: a ``requires_db`` service whose class cannot be derived.
    """
    if spec.db_access is not None:
        return spec.db_access
    if not spec.requires_db:
        return None
    marker = profile_marker(spec)
    if marker == "gateway":
        return "gateway"
    if marker in {"runner", "agent"}:
        return "runner"
    if spec.capabilities == _GATEWAY:
        return "gateway"
    if spec.capabilities == _AGENT_RUNNER:
        return "runner"
    raise ValueError(
        f"service {spec.session!r} uses the database but declares no db_access and "
        "its capabilities do not decide one"
    )


def api_access(spec: ServiceSpec) -> DbAccess | None:
    """The machine API token class the launcher delivers to ``spec``.

    A database service's API class is its login class (``db_access``). Any
    other service's follows its profile marker or its single capability, like
    the login class; a service carrying both capabilities without a declared
    class (the telemetry collector, whose relay bearer is rendered into its
    configuration) receives no token.
    """
    declared = db_access(spec)
    if declared is not None:
        return declared
    marker = profile_marker(spec)
    if marker == "gateway" or (marker is None and spec.capabilities == _GATEWAY):
        return "gateway"
    if marker in {"runner", "agent"} or (marker is None and spec.capabilities == _AGENT_RUNNER):
        return "runner"
    return None
