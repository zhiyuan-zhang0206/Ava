"""This host's stable machine identifier + cluster role — the two core
labels of a multi-machine deployment.

Multi-machine deployment (public TLS central PG/Redis on VPS + per-machine
steward agent syncing the memory git repo): manual setup once per host;
we do **not** read `socket.gethostname()` (drifts on macOS when switching
wifi).

- `machine_name()`: stable identifier — memory git branch
  (`machine-<name>`) / `agents_meta.machine` / `machines` table PK all
  read from this.
- `machine_role()`: this host's capability SET (frozenset). `gateway` =
  owns PG/Redis + the HTTP gateway + all gateway daemons; `agent-runner` =
  runs agent-host+ops+watchdog and disposable tool children (its DB/Redis/Milvus URLs
  point at a gateway node); `observability-station` = owns the native LGTM
  observability backends (the declarative form of the `$AVA_HOME/lgtm-host`
  marker). A host carries any combination; a single-box deployment is
  `gateway,agent-runner`. `ava start` brings up the union of the services
  each capability needs. Prefer `is_gateway()` / `is_agent_runner()` /
  `is_observability_station()` over comparing the set directly.

Source: the home's `.env` (read through Settings), nothing else.
- machine_name: `AVA_MACHINE_NAME`, else MachineNameMissing.
- machine_role: derived from independent capability flags, each the bool of
  `AVA_MACHINE_SERVE_GATEWAY` / `AVA_MACHINE_SERVE_AGENT_RUNNER` /
  `AVA_MACHINE_SERVE_OBSERVABILITY_STATION` (unset = off). A host with no
  capability raises MachineRoleMissing.

`ava init --machine-name X --serve-gateway` records these in the home's `.env`;
if no capability is set, a start fails loud and prints an actionable hint.
**No** TTY prompt — agent-first design, agent has no TTY and would hang.
"""

import enum
from collections.abc import Iterable
from typing import Literal

from base.cluster.auth import bearer_header, delivered_token
from base.config import settings
from base.host.env.dotenv_boot import launcher_context

# A machine carries a SET of capabilities, not a single role. `gateway` owns
# Postgres/Redis + the HTTP gateway; `agent-runner` runs agent processes;
# `observability-station` owns the native LGTM observability backends. A
# single-box deployment carries gateway+agent-runner. Each capability is
# declared independently by its own boolean flag (`AVA_MACHINE_SERVE_*`, unset =
# off); `_resolve_role` collapses the booleans into the frozenset.
# The DB still encodes the set as a TEXT[] of capability tokens. `MachineRole`
# is one capability; `MachineRoles` is the set machine_role() returns.
MachineRole = Literal["gateway", "agent-runner", "observability-station"]
# The set type is frozenset[str] (not frozenset[MachineRole]): values flow in
# from the DB (TEXT[]) and a comma-separated env string, and frozenset is
# invariant, so a literal `frozenset({"gateway"})` would not be assignable to a
# frozenset[Literal]. Membership is validated at runtime by _parse_roles.
MachineRoles = frozenset[str]
_VALID_CAPABILITIES: frozenset[str] = frozenset(
    ("gateway", "agent-runner", "observability-station")
)


class MachineNameMissing(RuntimeError):  # noqa: N818 — "state description" naming, same as AgentNotFound / IndexerUnavailable
    """`AVA_MACHINE_NAME` is not set — multi-machine setup is incomplete."""


class MachineRoleMissing(RuntimeError):  # noqa: N818
    """This host serves no capability (every AVA_MACHINE_SERVE_* flag off or unset) — multi-machine setup is incomplete."""


class MachineRoleInvalid(ValueError):  # noqa: N818
    """role contains a token that is not 'gateway' / 'agent-runner' / 'observability-station', or is empty — typo or wrong input."""


class GatewayApiBaseMissing(RuntimeError):  # noqa: N818 — state description, same style as MachineNameMissing
    """gateway_url unset — cannot resolve where to reach the gateway."""


class GatewayApiTokenMissing(RuntimeError):  # noqa: N818 — state description, same style as GatewayApiBaseMissing
    """An agent- or runner-profile process has no machine API token although the cluster API is authenticated."""


class _Unset(enum.Enum):
    TOKEN = enum.auto()


_UNSET = _Unset.TOKEN


class _IdentityHolder:
    """Process-level machine identity cache. Each field resolves lazily and
    independently (a caller needing only role never triggers name resolution),
    sharing one reset() for test isolation and one set() for explicit injection.
    Replaces a per-function lru_cache so callers inject a value instead of
    monkeypatching settings + busting caches.
    """

    def __init__(self) -> None:
        self._name: str | None = None
        self._role: MachineRoles | None = None
        self._description: str | None = None
        self._description_resolved: bool = False
        self._host: str | None = None

    def name(self) -> str:
        if self._name is None:
            self._name = _resolve_name()
        return self._name

    def role(self) -> MachineRoles:
        if self._role is None:
            self._role = _resolve_role()
        return self._role

    def host(self) -> str:
        if self._host is None:
            self._host = _resolve_host()
        return self._host

    def description(self) -> str | None:
        if not self._description_resolved:
            self._description = _resolve_description()
            self._description_resolved = True
        return self._description

    def set(
        self,
        *,
        name: str | _Unset = _UNSET,
        role: str | Iterable[str] | _Unset = _UNSET,
        description: str | _Unset | None = _UNSET,
        host: str | _Unset = _UNSET,
    ) -> None:
        if name is not _UNSET:
            self._name = name
        if role is not _UNSET:
            self._role = _coerce_roles(role)
        if description is not _UNSET:
            self._description = description
            self._description_resolved = True
        if host is not _UNSET:
            self._host = host

    def reset(self) -> None:
        self._name = None
        self._role = None
        self._description = None
        self._description_resolved = False
        self._host = None


_identity = _IdentityHolder()


def machine_name() -> str:
    """Get this host's identifier from settings (`AVA_MACHINE_NAME`).

    Raises:
        MachineNameMissing: settings.general.machine_name is empty.
    """
    return _identity.name()


def machine_role() -> MachineRoles:
    """Get this host's capability set, from the `AVA_MACHINE_SERVE_*` settings.

    Returns a frozenset of capabilities, each `gateway`, `agent-runner`, or
    `observability-station`. Prefer `is_gateway()` / `is_agent_runner()` /
    `is_observability_station()` at branch sites.

    Raises:
        MachineRoleMissing: not set.
        MachineRoleInvalid: set but contains an unknown token / is empty.
    """
    return _identity.role()


def is_gateway() -> bool:
    """True when this host carries the `gateway` capability (owns the data plane
    + HTTP gateway). Raises MachineRoleMissing/Invalid like machine_role()."""
    return "gateway" in machine_role()


def is_agent_runner() -> bool:
    """True when this host carries the `agent-runner` capability (runs agent
    processes). Raises MachineRoleMissing/Invalid like machine_role()."""
    return "agent-runner" in machine_role()


def is_observability_station() -> bool:
    """True when this host carries the `observability-station` capability (owns
    the native LGTM observability backends — the declarative form of the
    `$AVA_HOME/lgtm-host` marker). Raises MachineRoleMissing/Invalid like
    machine_role()."""
    return "observability-station" in machine_role()


def format_capabilities(
    serve_gateway: bool,  # noqa: FBT001 — capability flags, mirror is_gateway/is_agent_runner
    serve_agent_runner: bool,  # noqa: FBT001
    serve_observability_station: bool = False,  # noqa: FBT001, FBT002 — default keeps old callers' label
) -> str:
    """Render the capability flags as one human label.

    "gateway + agent-runner" on a single-box host, "gateway" or "agent-runner"
    for a split node, "observability-station" for a pure station, "none" when
    a host serves none. Single source for the `ava status` serves: line and
    the `ava cluster status` role column, which both display the capability
    set (there is no single categorical role field on the wire — see
    MachineStatus / ClusterStatus). The third flag defaults False so a caller
    that only knows the two original flags keeps the pre-station label.
    """
    caps = [
        name
        for name, on in (
            ("gateway", serve_gateway),
            ("agent-runner", serve_agent_runner),
            ("observability-station", serve_observability_station),
        )
        if on
    ]
    return " + ".join(caps) or "none"


def machine_description() -> str | None:
    """Get this host's free-text description (settings, `AVA_MACHINE_DESCRIPTION`) or None.

    Unlike machine_name / machine_role, absence is legal: a machine without a
    description is valid, so this returns None instead of raising.
    """
    return _identity.description()


def reachable_host() -> str:
    """Get this host's reachable address on the cluster's private network.

    The address other cluster nodes and the user's browser dial — used to build
    direct page URLs (`ava.ui.show` -> `http://<host>:<port>/`) and, on a gateway,
    the data-plane endpoint enrolled agent-runners connect to.

    Precedence:
    1. `settings.general.machine_host` (env `AVA_MACHINE_HOST`, which
       `ava init --machine-host` records in `.env`), when non-empty
    2. `localhost` — a single box is reachable only at loopback (zero-config).

    The operator declares this address; it is not auto-detected, so the codebase
    makes no assumption about how the machines reach each other (VPN / LAN /
    overlay network / etc.). A misconfigured remote host that falls through
    to `localhost` is caught downstream by the loopback guard in
    `base.cluster.machines.register_self`, not by this resolver.

    Resolved once per process and cached.
    """
    return _identity.host()


def set_identity(
    *,
    name: str | _Unset = _UNSET,
    role: str | Iterable[str] | _Unset = _UNSET,
    description: str | _Unset | None = _UNSET,
    host: str | _Unset = _UNSET,
) -> None:
    """Inject machine identity, overriding settings resolution for the fields
    provided. Fields left unset keep their current resolved/injected value.
    Used by tests and by explicit bootstrap. Call reset_identity() to clear.
    """
    _identity.set(name=name, role=role, description=description, host=host)


def reset_identity() -> None:
    """Clear all injected/resolved identity; the next read re-resolves from
    settings.
    """
    _identity.reset()


def _resolve_name() -> str:
    env = settings.general.machine_name.strip()
    if env:
        return env
    raise MachineNameMissing(
        "machine name not set — `ava init --machine-name <name>` records it for a new "
        "home; for an initialized one set AVA_MACHINE_NAME in its `.env` (e.g. host-a / host-b)."
    )


def _resolve_role() -> MachineRoles:
    serve_gateway = bool(settings.general.machine_serve_gateway)
    serve_agent_runner = bool(settings.general.machine_serve_agent_runner)
    serve_observability_station = bool(settings.general.machine_serve_observability_station)
    caps = {
        cap
        for cap, on in (
            ("gateway", serve_gateway),
            ("agent-runner", serve_agent_runner),
            ("observability-station", serve_observability_station),
        )
        if on
    }
    if not caps:
        raise MachineRoleMissing(
            "this host serves neither gateway, agent-runner, nor observability-station — set "
            "AVA_MACHINE_SERVE_GATEWAY and/or AVA_MACHINE_SERVE_AGENT_RUNNER and/or "
            "AVA_MACHINE_SERVE_OBSERVABILITY_STATION (or run `ava init --serve-gateway` / "
            "`--serve-agent-runner` / `--serve-observability-station` on a new home; a single "
            "box serves both gateway and agent-runner)."
        )
    return frozenset(caps)


def _resolve_description() -> str | None:
    return settings.general.machine_description.strip() or None


def _resolve_host() -> str:
    env = settings.general.machine_host.strip()
    if env:
        return env
    # Zero-config single box: reachable only at loopback. A remote runner that
    # wrongly lands here is rejected at registration time by the loopback guard in
    # base.cluster.machines.register_self, so this never silently registers a
    # self-dialing address.
    return "localhost"


def _resolve_gateway_url() -> str | None:
    """Resolve the configured gateway base URL (`AVA_GATEWAY_URL`) or None.
    Trailing slash stripped.

    The single source both gateway_api_base() (this module) and
    base.cluster.machines.gateway_url() read, so the "where is the gateway" answer
    never drifts between them.
    """
    env = settings.gateway.gateway_url.strip()
    return env.rstrip("/") if env else None


def gateway_api_base() -> str:
    """Base URL this unit uses to call the gateway over HTTP.

    One role-blind resolver for every client-side caller (the agent SDK, the
    monitor wrapper, the runner, the watchdog, and dev bootstrap): the configured
    gateway address, `AVA_GATEWAY_URL`, else raise. A gateway-capable host resolves
    the *same* configured address as any other caller — on a single box that address is the
    box's own reachable address, so its agent-runner half dials its gateway half
    the same way a remote runner would. No localhost shortcut: single-box and
    split deployments share one path.

    Raises:
        GatewayApiBaseMissing: gateway_url unset.
    """
    url = _resolve_gateway_url()
    if url is None:
        raise GatewayApiBaseMissing(
            "gateway_url unset — `ava start` writes it on an agent-runner; "
            "or `export AVA_GATEWAY_URL=<gateway url>` (a gateway host sets its "
            "own reachable URL there too)."
        )
    return url


# Launcher profiles whose processes present the machine API token and never the
# human secret: the launcher delivers one to every agent host and runner service
# (`cli.commands.data_plane.bringup.api_delivery`), exec children inherit it.
_TOKEN_ONLY_PROFILES = frozenset({"agent", "runner"})


def gateway_bearer() -> str:
    """The bearer this process presents to the gateway API; empty in the open posture.

    The machine API token of the active write generation first: delivered by
    the launcher, or by the boot pass to an admitted operator process. Without
    one, the human cluster secret (an operator on the gateway home; a
    gateway-profile service) — except in an agent- or runner-profile process,
    which holds its token whenever the cluster API is authenticated, so a
    missing token there is a launch defect and raises instead of silently
    presenting the human secret. A remote-managed data plane keeps no write
    generations and delivers no token (`api_delivery`): its gateway-local
    services present the human secret, so that plane is exempt.

    Raises:
        GatewayApiTokenMissing: an agent- or runner-profile process has no
            `AVA_API_TOKEN` while `AVA_CLUSTER_SECRET` is set.
    """
    token = delivered_token()
    if token:
        return token
    secret = settings.data_plane.cluster_secret
    profile = launcher_context()
    if secret and profile in _TOKEN_ONLY_PROFILES and not _plane_delivers_no_tokens():
        raise GatewayApiTokenMissing(
            f"this {profile}-profile process carries no machine API token (AVA_API_TOKEN) "
            "while the cluster API is authenticated (AVA_CLUSTER_SECRET is set). The root "
            "launcher delivers the active write generation's token to every agent host and "
            "runner service it starts, and such a process never presents the human secret "
            "instead: restart the cluster's services (`ava stop`, then `ava start`) so the "
            "launcher delivers it."
        )
    return secret


def _plane_delivers_no_tokens() -> bool:
    """Whether this unit is a gateway home on a remote-managed data plane.

    The one case where `api_delivery` hands out no token although the API is
    authenticated: the plane keeps no write generations to issue one from.
    """
    from base.host.env.bootstrap import config_source_is_local

    return config_source_is_local() and settings.data_plane.is_remote


def gateway_auth_headers() -> dict[str, str]:
    """Auth headers a client presents to the cluster's authenticated surfaces.

    The Bearer paired with `gateway_api_base` for every client-side call to an
    authenticated gateway route (`/api/cluster/*`, `/api/agents/*`,
    `/api/config`, `/api/memory/*`, ...) and with the gateway's `/ops` dials:
    `gateway_bearer()`. Empty in the open posture, where the middleware is a
    no-op, so the same call site works either way.

    Raises:
        GatewayApiTokenMissing: see `gateway_bearer`.
    """
    bearer = gateway_bearer()
    return bearer_header(bearer) if bearer else {}


def _parse_roles(value: str) -> MachineRoles:
    """Parse a comma-separated capability string into a validated frozenset.

    `gateway,agent-runner` -> frozenset({'gateway', 'agent-runner'}). Each token
    must be a known capability; an empty set (no valid tokens) is rejected so a
    blank / all-comma value fails loud rather than resolving to "no capability".
    """
    tokens = frozenset(t.strip() for t in value.split(",") if t.strip())
    unknown = tokens - _VALID_CAPABILITIES
    if unknown or not tokens:
        raise MachineRoleInvalid(
            f"machine_role={value!r} invalid; comma-separated tokens, each "
            f"'gateway', 'agent-runner', or 'observability-station' (got unknown {sorted(unknown)!r})."
            if unknown
            else f"machine_role={value!r} invalid; needs at least one of "
            "'gateway' / 'agent-runner' / 'observability-station'."
        )
    return tokens


def _coerce_roles(role: str | Iterable[str]) -> MachineRoles:
    """Normalize an injected role into a validated frozenset. A string is parsed
    as comma-separated (`set_identity(role="gateway,agent-runner")`); any other
    iterable of capability tokens is validated the same way."""
    if isinstance(role, str):
        return _parse_roles(role)
    return _parse_roles(",".join(role))
