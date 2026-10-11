"""Explicitly owned machine identity and cluster capability values.

Composition roots supply four independent lazy readers. Each MachineIdentity
caches its own name, capabilities, reachable host and optional description;
resetting one owner never changes another. The startup factory is defined in
base.cluster.machine.boot and consumes the root's ConfigBoot without reading
any fields at construction. SDK consumers use the machine in their AvaContext.

Machine names are stable deployment labels, never inferred from hostnames.
Capabilities are independently declared gateway, agent-runner and
observability-station flags; a host runs their union. Missing name/role and
invalid capability tokens retain their explicit errors. Gateway URL and bearer
helpers validate the existing client credential policy independently of identity.
"""

import enum
from collections.abc import Callable, Iterable
from typing import Literal

from base.cluster.auth import bearer_header, delivered_token
from base.host.env.dotenv_boot import launcher_context

__all__ = [
    "GatewayApiBaseMissing",
    "GatewayApiTokenMissing",
    "MachineIdentity",
    "MachineNameMissing",
    "MachineRole",
    "MachineRoleInvalid",
    "MachineRoleMissing",
    "MachineRoles",
    "daemon_acceptance",
    "format_capabilities",
    "gateway_api_base",
    "gateway_auth_headers",
    "gateway_bearer",
    "resolve_gateway_api_base",
    "resolve_gateway_bearer",
    "roles_from_flags",
    "validate_machine_name",
]

# A machine carries a SET of capabilities, not a single role. `gateway` owns
# Postgres/Redis + the HTTP gateway; `agent-runner` runs agent processes;
# `observability-station` owns the native LGTM observability backends. A
# single-box deployment carries gateway+agent-runner. Each capability is
# declared independently by its own boolean flag (`AVA_MACHINE_SERVE_*`, unset =
# off); `roles_from_flags` collapses the booleans into the frozenset.
# The DB still encodes the set as a TEXT[] of capability tokens. `MachineRole`
# is one capability; `MachineRoles` is the set MachineIdentity.role() returns.
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


class MachineIdentity:
    """An explicitly owned machine identity cache. Each field resolves lazily and
    independently (a caller needing only role never triggers name resolution),
    sharing one reset() for test isolation and one set() for explicit injection.
    Replaces a per-function lru_cache so callers inject a value instead of
    monkeypatching settings + busting caches.
    """

    def __init__(
        self,
        *,
        name: Callable[[], str],
        role: Callable[[], MachineRoles],
        host: Callable[[], str],
        description: Callable[[], str | None],
    ) -> None:
        self._read_name = name
        self._read_role = role
        self._read_host = host
        self._read_description = description
        self._name: str | None = None
        self._role: MachineRoles | None = None
        self._description: str | None = None
        self._description_resolved: bool = False
        self._host: str | None = None

    def name(self) -> str:
        if self._name is None:
            self._name = validate_machine_name(self._read_name())
        return self._name

    def role(self) -> MachineRoles:
        if self._role is None:
            self._role = _coerce_roles(self._read_role())
        return self._role

    def host(self) -> str:
        if self._host is None:
            self._host = self._read_host().strip() or "localhost"
        return self._host

    def description(self) -> str | None:
        if not self._description_resolved:
            description = self._read_description()
            self._description = description.strip() or None if description else None
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


def validate_machine_name(value: str) -> str:
    """Normalize a configured name and reject an uninitialized machine."""
    env = value.strip()
    if env:
        return env
    raise MachineNameMissing(
        "machine name not set — `ava init --machine-name <name>` records it for a new "
        "home; for an initialized one set AVA_MACHINE_NAME in its `.env` (e.g. host-a / host-b)."
    )


def roles_from_flags(
    *,
    serve_gateway: bool,
    serve_agent_runner: bool,
    serve_observability_station: bool,
) -> MachineRoles:
    """Resolve the declared capability set without reading process settings."""
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


def _normalize_gateway_url(value: str) -> str | None:
    value = value.strip()
    return value.rstrip("/") if value else None


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
    from base.config import settings

    return resolve_gateway_api_base(settings.gateway.gateway_url)


def resolve_gateway_api_base(value: str) -> str:
    """Validate an explicitly owned gateway URL with the canonical resolver rules."""
    url = _normalize_gateway_url(value)
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
    from base.config import settings

    return resolve_gateway_bearer(
        token_reader=delivered_token,
        secret_reader=lambda: settings.data_plane.cluster_secret,
        profile_reader=launcher_context,
        no_tokens_reader=_plane_delivers_no_tokens,
    )


def resolve_gateway_bearer(
    *,
    token_reader: Callable[[], str | None],
    secret_reader: Callable[[], str],
    profile_reader: Callable[[], str | None],
    no_tokens_reader: Callable[[], bool],
) -> str:
    """Read one owner's credential policy, preserving token-first lazy admission."""
    token = token_reader()
    if token:
        return token
    secret = secret_reader()
    profile = profile_reader()
    if secret and profile in _TOKEN_ONLY_PROFILES and not no_tokens_reader():
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
    from base.config import settings
    from base.host.env.bootstrap import config_source_is_local

    return config_source_is_local() and settings.data_plane.is_remote


def daemon_acceptance() -> frozenset[str] | None:
    """SHA-256 digests of the bearers a gateway-side daemon's authenticated endpoint accepts,
    or None for the open posture. The acceptor's half of `gateway_bearer()`: the daemons that
    take calls from the gateway (the ops server, im_bridge's `/send`) accept machine API tokens,
    never the human cluster secret.

    The gateway dials with its gateway-class token and a runner-class process of the unit with its
    runner token, so a daemon accepts exactly its write generation's two tokens. A remote unit
    takes the gateway token digest its installed capability carries plus its own runner token's
    (none when its cluster's API is open): it never holds the gateway token or the human secret.
    The gateway home takes the active generation's two tokens while the cluster secret is set
    (none while no generation is active: fail closed).

    The one place the human secret is still accepted is a remote-managed data plane
    (`settings.data_plane.is_remote`): it keeps no write generations and issues no token, so
    its gateway-local services present, and its daemons accept, the human secret. That branch
    is the exception to "outside the gateway, the CLI and the root, no component holds the
    cluster secret" (docs/decisions/data/security/2026-10-03-cluster-secret-contraction.md); the live
    deployment's data plane is local, and this branch only runs when `is_remote`.
    """
    from base.cluster.authority.api import acceptance, token_digest
    from base.cluster.authority.unit import load_unit_capability
    from base.config import settings
    from base.paths import ava_home

    home = ava_home().resolve()
    capability = load_unit_capability(home)
    if capability is not None:
        api = capability.api
        return None if api is None else frozenset({api.gateway, token_digest(api.token)})
    secret = settings.data_plane.cluster_secret
    if not secret:
        return None
    if settings.data_plane.is_remote:
        return frozenset({token_digest(secret)})
    return frozenset(acceptance(home).values())


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
