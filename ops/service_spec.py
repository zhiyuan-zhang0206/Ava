"""Service desired-state contracts and process-profile derivation."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from shared.daemon_health import DaemonProbe
from shared.machine import MachineRole

# The capability groups a service can belong to. These are `MachineRole` values
# (`machine_role()` returns a frozenset of them); "capability" and "role" are the
# same axis in Ava's vocabulary (a unit carries a SET of capabilities).
_GATEWAY: frozenset[MachineRole] = frozenset({"gateway"})
_AGENT_RUNNER: frozenset[MachineRole] = frozenset({"agent-runner"})
_BOTH: frozenset[MachineRole] = frozenset({"gateway", "agent-runner"})


@dataclass(frozen=True)
class ServiceSpec:
    """Desired state for a single service.

    Attributes:
        session: bare service kebab (e.g. ``gateway``, ``frontend``); the real
            session name is composed by ``shared.cluster.session_name``.
        cmd: shell command run in the session (wrapped in ``cd <repo> && ...``).
        capabilities: which machine capabilities run this service. A host runs the
            service iff its role set intersects this — so a gateway-only host runs
            the ``{"gateway"}`` services, an agent-runner-only host runs the
            ``{"agent-runner"}`` services, and a single box (both roles) runs the
            union. This is the single readable place that says "which machine runs
            this", replacing the old exclusion-set encoding.
        requires_db: whether this service reads or writes the cluster's Postgres.
            Deliberately REQUIRED (no default): the watchdog holds back exactly the
            ``True`` services when a controller reports a DB-scoped round block
            (``BlockScope.DB_DEPENDENT`` — DB unreachable, or applied migrations
            disagreeing with this checkout), because reviving one of those would
            spawn a daemon that dies in its own ``assert_schema_current`` and
            crash-loops once a round. A default would silently classify the next
            service for the author, which is the coupling this field exists to
            remove — so a new service states the answer where it is declared, and a
            DB-free one keeps being revived through a DB outage. This is a fact about
            the SERVICE, not about its healthcheck, so the two watchdog specs answer
            it too even though nothing filters on them.
        pidfile: pidfile path (None = no pidfile, probe via other means).
        healthcheck_module: the ``services.healthchecks.<x>`` module whose
            ``main()`` the watchdog imports and runs every 60s to keep this
            service alive — the keepalive roster is DERIVED from this field. None
            = not watchdog-monitored (the watchdog daemons themselves).
        curl_url: HTTP probe URL (2xx/3xx = up); None = no curl probe.
        tcp_port: TCP-connect probe port for non-HTTP services (milvus gRPC).
        identity_probe: the probe that answers "is the thing on that port MINE",
            returning a ``DaemonProbe`` verdict. Set for every service whose
            endpoint can prove it — the ``/healthz`` daemons (name + home + pid,
            ``probe_daemon``), the gateway (home only, ``probe_home`` — uvicorn's
            reload fork makes the pid meaningless), and the browser (profile +
            listening socket, ``services.browser.probe``, because CDP carries no
            field we control). **None means the probe can assert liveness and
            nothing more**, and that is a property of the endpoint, not an
            oversight: the frontend serves Next.js, milvus speaks gRPC, and a
            watchdog's only signal is its own pidfile. Consumers show which of the
            two a row got, so an operator can tell an identity-verified ✓ from a
            2xx (`cli.commands._probe`). It exists as a field rather than being
            re-derived per consumer: the alternative let watchdog verify identity
            while operator surfaces trusted a bare 2xx from an occupant.
        gate: optional predicate returning a gate reason (a string = gated OUT of
            the start roster + why, None = will start). When set it OVERRIDES the
            built-in ``_gate_reason`` lookup, so a plugin service carries its own
            domain gate instead of adding a central branch; core services still
            flow through ``_gate_reason``.
        before_launch: optional preflight run immediately before creating the
            session, for a service-specific safe takeover.
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
            labeler's watchdog respawn path already makes the same choice
            (services/healthchecks/labeler.py) — this field makes the initial
            ``ava start`` spawn agree with it.
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
    before_launch: Callable[[], None] | None = None
    profile: str | None = None
    no_profile_marker: bool = False


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
