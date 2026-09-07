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

Precedence:
- machine_name: env `AVA_MACHINE_NAME` > `$AVA_HOME/machine_name` file >
  MachineNameMissing.
- machine_role: derived from two independent capability flags, each resolved
  env `AVA_MACHINE_SERVE_GATEWAY` / `AVA_MACHINE_SERVE_AGENT_RUNNER` (bool) >
  `$AVA_HOME/machine_serve_gateway` / `machine_serve_agent_runner` file >
  False. A host with no capability raises MachineRoleMissing.

When first passing `ava start --machine-name X --serve-gateway`, the CLI writes
the files; if neither capability is set, it fails loud and prints an actionable
hint. **No** TTY prompt — agent-first design, agent has no TTY and would hang.
"""

import enum
from collections.abc import Iterable
from typing import Literal

from shared.cluster_auth import bearer_header
from shared.config import settings
from shared.paths import ava_home

# A machine carries a SET of capabilities, not a single role. `gateway` owns
# Postgres/Redis + the HTTP gateway; `agent-runner` runs agent processes;
# `observability-station` owns the native LGTM observability backends. A
# single-box deployment carries gateway+agent-runner. Each capability is
# declared independently by its own boolean flag (env `AVA_MACHINE_SERVE_*` >
# the matching `$AVA_HOME/machine_serve_*` file > False); `_resolve_role`
# collapses the booleans into the frozenset.
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
    """Neither env `AVA_MACHINE_NAME` nor `$AVA_HOME/machine_name` is set — multi-machine setup is incomplete."""


class MachineRoleMissing(RuntimeError):  # noqa: N818
    """This host serves neither gateway nor agent-runner (both AVA_MACHINE_SERVE_* flags + files off) — multi-machine setup is incomplete."""


class MachineRoleInvalid(ValueError):  # noqa: N818
    """role contains a token that is not 'gateway' / 'agent-runner', or is empty — typo or wrong input."""


class GatewayApiBaseMissing(RuntimeError):  # noqa: N818 — state description, same style as MachineNameMissing
    """gateway_url unset (env and file both absent) — cannot resolve where to reach the gateway."""


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
        description: str | None | _Unset = _UNSET,
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
    """Get this host's identifier. settings (env-backed) > file > raise.

    Raises:
        MachineNameMissing: settings.general.machine_name empty + file missing or empty.
    """
    return _identity.name()


def machine_role() -> MachineRoles:
    """Get this host's capability set. settings (env-backed) > file > raise.

    Returns a frozenset of capabilities, each `gateway` or `agent-runner`; a
    single-box host carries both. Prefer `is_gateway()` / `is_agent_runner()`
    at branch sites.

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
    """Get this host's free-text description. settings (env-backed) > file > None.

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
    1. `settings.general.machine_host` (env `AVA_MACHINE_HOST`), when non-empty
    2. `$AVA_HOME/machine_host` file (written by `ava enroll --machine-host`)
    3. `localhost` — a single box is reachable only at loopback (zero-config).

    The operator declares this address; it is not auto-detected, so the codebase
    makes no assumption about how the machines reach each other (VPN / LAN /
    overlay network / etc.). The `localhost` fallback is deliberately last so an
    enrolled agent-runner's `$AVA_HOME/machine_host` file wins over it — a
    non-empty default here would shadow the file and register the runner at a
    self-dialing loopback address. A misconfigured remote host that falls through
    to `localhost` is caught downstream by the loopback guard in
    `shared.machines.register_self`, not by this resolver.

    Resolved once per process and cached.
    """
    return _identity.host()


def set_identity(
    *,
    name: str | _Unset = _UNSET,
    role: str | Iterable[str] | _Unset = _UNSET,
    description: str | None | _Unset = _UNSET,
    host: str | _Unset = _UNSET,
) -> None:
    """Inject machine identity, overriding env/file resolution for the fields
    provided. Fields left unset keep their current resolved/injected value.
    Used by tests and by explicit bootstrap. Call reset_identity() to clear.
    """
    _identity.set(name=name, role=role, description=description, host=host)


def reset_identity() -> None:
    """Clear all injected/resolved identity; the next read re-resolves from
    env/file.
    """
    _identity.reset()


def _resolve_name() -> str:
    env = settings.general.machine_name.strip()
    if env:
        return env
    p = ava_home() / "machine_name"
    if p.exists():
        name = p.read_text().strip()
        if name:
            return name
    raise MachineNameMissing(
        f"machine name not set — run `ava start --machine-name <name>` once "
        f"(writes {p}); or `export AVA_MACHINE_NAME=<name>` (e.g. host-a / host-b)."
    )


def parse_serve_value(value: str, source: str) -> bool:
    """Parse a capability flag's textual value (a `$AVA_HOME/machine_serve_*` file)
    strictly. Blank is off; a recognized true / false token resolves; anything else
    raises. A typo (`treu`, `gateway`, `2`) must NOT silently become False — that
    would start the host the wrong shape (e.g. gateway-only when both were meant)
    while looking healthy.

    Raises:
        MachineRoleInvalid: a non-blank value that is not a true/false token.
    """
    v = value.strip().lower()
    if not v:
        return False
    if v in ("1", "true", "yes", "on"):
        return True
    if v in ("0", "false", "no", "off"):
        return False
    raise MachineRoleInvalid(
        f"{source} has unrecognized value {value.strip()!r} — expected true/false"
    )


def _resolve_serve(env_value: bool | None, file_name: str) -> bool:  # noqa: FBT001 — tri-state capability flag, passed by name from _resolve_role
    """Resolve one capability flag: env (settings bool) > `$AVA_HOME/<file>` > False.
    None means the env var is unset (fall through to the file); an explicit
    true/false in either source is honored (a malformed file value raises)."""
    if env_value is not None:
        return env_value
    p = ava_home() / file_name
    if p.exists():
        return parse_serve_value(p.read_text(), str(p))
    return False


def _resolve_role() -> MachineRoles:
    serve_gateway = _resolve_serve(settings.general.machine_serve_gateway, "machine_serve_gateway")
    serve_agent_runner = _resolve_serve(
        settings.general.machine_serve_agent_runner, "machine_serve_agent_runner"
    )
    serve_observability_station = _resolve_serve(
        settings.general.machine_serve_observability_station,
        "machine_serve_observability_station",
    )
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
            "AVA_MACHINE_SERVE_OBSERVABILITY_STATION (or run `ava start --serve-gateway` / "
            "`--serve-agent-runner` / `--serve-observability-station`; a single box serves both "
            "gateway and agent-runner)."
        )
    return frozenset(caps)


def _resolve_description() -> str | None:
    env = settings.general.machine_description.strip()
    if env:
        return env
    p = ava_home() / "machine_description"
    if p.exists():
        text = p.read_text().strip()
        if text:
            return text
    return None


def _resolve_host() -> str:
    env = settings.general.machine_host.strip()
    if env:
        return env
    p = ava_home() / "machine_host"
    if p.exists():
        host = p.read_text().strip()
        if host:
            return host
    # Zero-config single box: reachable only at loopback. This fallback is last
    # so the `machine_host` file (written by `ava enroll --machine-host`) wins —
    # the config field's default is empty for the same reason (a non-empty
    # default would shadow the file). A remote runner that wrongly lands here is
    # rejected at registration time by the loopback guard in
    # shared.machines.register_self, so this never silently registers a
    # self-dialing address.
    return "localhost"


def _resolve_gateway_url() -> str | None:
    """Resolve the configured gateway base URL: env `AVA_GATEWAY_URL` > file
    `$AVA_HOME/gateway_url` > None. Trailing slash stripped.

    The single source both gateway_api_base() (this module) and
    shared.machines.gateway_url() read, so the "where is the gateway" answer
    never drifts between them.
    """
    env = settings.gateway.gateway_url.strip()
    if env:
        return env.rstrip("/")
    p = ava_home() / "gateway_url"
    if p.exists():
        url = p.read_text().strip()
        if url:
            return url.rstrip("/")
    return None


def gateway_api_base() -> str:
    """Base URL this unit uses to call the gateway over HTTP.

    One role-blind resolver for every client-side caller (the agent SDK, the
    monitor wrapper, the runner, the watchdog, and
    dev bootstrap): the configured gateway address, env `AVA_GATEWAY_URL` > file
    `$AVA_HOME/gateway_url` > raise. A gateway-capable host resolves the *same*
    configured address as any other caller — on a single box that address is the
    box's own reachable address, so its agent-runner half dials its gateway half
    the same way a remote runner would. No localhost shortcut: single-box and
    split deployments share one path.

    Raises:
        GatewayApiBaseMissing: gateway_url unset (env and file both absent).
    """
    url = _resolve_gateway_url()
    if url is None:
        raise GatewayApiBaseMissing(
            "gateway_url unset — `ava enroll` writes it on an agent-runner; "
            "or `export AVA_GATEWAY_URL=<gateway url>` (a gateway host sets its "
            "own reachable URL there too)."
        )
    return url


def gateway_auth_headers() -> dict[str, str]:
    """Auth headers a client presents to the gateway's authenticated surfaces.

    The cluster secret as a Bearer token, paired with `gateway_api_base` for every
    client-side call to an authenticated gateway route (`/api/cluster/*`,
    `/api/agents/*`, `/api/config`, `/api/memory/*`, ...) — which the gateway's auth
    middleware requires on every route but the bypass set (`/api/health`,
    `/api/auth/*`) once a cluster secret is set. Empty when no secret is set
    (dev/test), where the middleware is a no-op, so the same call site works either way.
    """
    return (
        bearer_header(settings.data_plane.cluster_secret)
        if settings.data_plane.cluster_secret
        else {}
    )


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
