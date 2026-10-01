"""The env-key registry and its projections (R2 design, convergence point A).

Every env key this system forwards, forces, or drops is declared here or
in the Settings class metadata — except removable provider keys, whose enabled
plugin binding is their declaration:

- **Settings fields** declare themselves in `base/config/<domain>.py`
  (`json_schema_extra` metadata); `base/host/env/config_registry.py` builds the flat
  field registry from class metadata only (no Settings instantiation), and
  every projection below is a pure function of it — a new cluster-scoped field
  is force/dropped by the env-authority pass, forwarded to sessions, and
  distributed via /api/bootstrap with no hand-written set edit (the "env
  allowlist six-gap" incident class is structurally impossible: A3).
  The projections `load_ava_env` runs BEFORE Settings exists (the env-authority
  force/drop families) read the generated boot-lite static index
  (`base/host/env/config_lite_table.py`) instead of the live registry — building the
  registry there would pull pydantic + all 15 sub-models into every boot
  (#3621); the index is generated from the same declarations and locked
  equal to the registry by tests/base/test_config_lite_table.py.
- **Non-Settings keys** (ambient display vars,
  overlay/birth JSON carriers, temp-dir vars, ...) are registered as
  `EnvField` passthrough rows below — one row per key (A1: exactly one
  declaration; a row whose key is also a Settings alias fails fast).

The old hand-written snapshots (`base/env_keys.py`, 12 definitions) are gone.
The authority for which projection a key lands in is the **consumption matrix**
(which process kind actually reads the key — the per-projection declarations
below); capability/scope metadata only validates (deriving env sets from
capability was the 2026-08-06 #1570 P0).

Projections (the design's boundary currency):
- `child_env(role)` — the parent->child forwarding view
  (SESSION/AGENT_FORWARD/HOST_PASSTHROUGH semantics);
  `role` reuses `AVA_PROCESS_PROFILE` (gateway/agent/runner) — daemons belong
  to the gateway/runner profiles.
- `env_keep_set(role)` / `env_authority_drop_set(role)` — the dotenv_boot
  env-authority force/drop families (set membership queries, not env dicts).

The derived sets use the generated static index and are memoized on first use.
The module stays importable before Settings exists: `dotenv_boot` runs its
authority pass at `.env`-load time. Provider-plugin declarations load lazily
only at the delivery boundaries that consume them.

Delivery is the backend env-dict handoff (`base.sessions.env_forwarding.forward_env_dict`).
KEY=VALUE argv delivery stays forbidden (secrets never ride
argv — decisions/2026-07-30-secrets-never-ride-argv.md).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from typing import Literal

from base.host.env.config_lite_table import (
    FIELD_ALIASES,
    FIELD_CAPABILITIES,
    FIELD_SCOPES,
)

# The process-profile roles `child_env` / the authority projections accept —
# the existing AVA_PROCESS_PROFILE vocabulary (base/config/profiles.py), not
# a new enum.
ProcessRole = Literal["gateway", "agent", "runner"]

# The certification proof is a host-local setting, but it is not a general
# host-child capability.  The root/service launchers project it only into the
# agent-host finalizer; model-facing sessions and agent children never receive
# it.  Keeping the alias here makes every boundary use the Settings declaration
# rather than a second string literal.
MANIFEST_CERTIFICATION_SECRET_ENV = FIELD_ALIASES[
    "impersonation_event_manifest_certification_secret"
]
# A targeted agent-host launch ticket, consumed before config boot. It is not a
# Settings field and never joins a generic child projection.
MANIFEST_CERTIFICATION_FINALIZER_ENV = "AVA_MANIFEST_CERTIFICATION_FINALIZER"

# ── Passthrough rows (A1: every non-Settings env key declared exactly once) ──


@dataclass(frozen=True)
class EnvField:
    """A registered env key that is not a Settings field — a passthrough row.

    `source="os-env"` means the key rides from the parent's os.environ as-is
    (never Settings-modeled, never defaulted). One row per key; a key that is
    also a Settings alias is a duplicate declaration and fails fast.
    """

    key: str
    source: Literal["os-env"] = "os-env"


# Ambient display / home vars a child must see BEFORE Settings: the X11/Wayland
# display sockets that display_available() -> browser_incapability() ->
# browser_capable() keys off, and $HOME, which POSIX tools (gh, git, ssh, ...)
# resolve their config dirs from (macOS bash 3.2 does NOT restore HOME in a
# login shell when it is absent from the inherited env — 2026-08-06 agents lost
# HOME and `gh auth status` flipped to "not logged in"). $USER / $LOGNAME are the
# same class: a login shell does not restore them either, and the macOS Claude
# Code CLI looks its keychain login up by account name — an agent PTY without
# USER read a logged-in CLI as "Not logged in" (2026-09-26 Mode B takeover).
# Forwarded non-empty only: an empty $DISPLAY means "no display", and
# forwarding "" would make a stripped display falsely look present in the child.
_HOST_PASSTHROUGH_ROWS = tuple(
    EnvField(key) for key in ("DISPLAY", "WAYLAND_DISPLAY", "HOME", "USER", "LOGNAME")
)
HOST_PASSTHROUGH_KEYS = frozenset(row.key for row in _HOST_PASSTHROUGH_ROWS)

# The machine's network proxy configuration (issue #2095). A service child that
# builds or installs (the frontend's `npm run build` fetching Google Fonts for
# next/font is the one that bit us) egresses through the environment these keys
# describe, and the positive allowlist used to drop them — so a host that
# reaches the network only through a proxy had no service child that could
# build. Both spellings ride: the machine's shell / Clash-style tooling exports
# the uppercase set, while npm, node and curl read the lowercase one. NO_PROXY
# is the operator's tool for keeping loopback and the private network direct.
#
# Values are machine-local by construction — the parent environment is the only
# source (an exporting shell, or `$AVA_HOME/.env` / `mirror.env`, which
# `load_ava_env` loads into os.environ before any child env is built), so no
# repo file ever carries an address. Copied non-empty only, like the display
# passthroughs above.
_NETWORK_PROXY_ROWS = tuple(
    EnvField(key)
    for key in (
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "NO_PROXY",
        "http_proxy",
        "https_proxy",
        "all_proxy",
        "no_proxy",
    )
)
NETWORK_PROXY_KEYS = frozenset(row.key for row in _NETWORK_PROXY_ROWS)


def network_proxy_configured() -> bool:
    """Whether the launching environment names a network proxy (either spelling).

    The single reader for `NETWORK_PROXY_KEYS`: `child_env` forwards whichever of
    the keys it finds, and a consumer that has to *decide* whether to leave a
    library's own proxy discovery alone (the Feishu ws handshake, issue #2089)
    asks this instead of re-listing the keys.
    """
    return any(os.environ.get(key) for key in NETWORK_PROXY_KEYS)


# The per-agent config overlay / birth stamp travel in env vars as JSON (never
# argv — issue #974: argv is world-readable via `ps`). Not Settings fields:
# they are process-bound carriers the exec launcher writes and child boot consumes.
AGENT_CONFIG_OVERLAY_ENV = "AVA_AGENT_CONFIG_OVERLAY"
AGENT_BIRTH_CONFIG_ENV = "AVA_AGENT_BIRTH_CONFIG"
# The Redis ACL runtime password is a file-only data-plane credential like the
# runner database password. derive_env emits it and process isolation strips it.
REDIS_PASSWORD_ENV = "AVA_REDIS_PASSWORD"  # noqa: S105 — env key, not a credential

# The home is not a Settings field (`base.host.env.dotenv_boot.resolve_ava_home`
# reads it before Settings exists): it is the one variable every descendant
# inherits, so a process tree that set it keeps every child on the same home.
AVA_HOME_ENV = "AVA_HOME"

# Bootstrap/identity guide keys an agent that self-fetches its config still
# needs forwarded before Settings: the gateway URL to reach /api/bootstrap; the
# data-plane URL as a boot-time fallback; the TLS bundle for the fetch on
# corp-MITM hosts. Plus the two JSON carriers above. The Settings aliases are
# declared by field name so an alias rename follows automatically; the rest are
# passthrough rows.
_GUIDE_FIELDS = frozenset({"cluster_secret", "gateway_url", "gateway_port"})
_GUIDE_PASSTHROUGH_KEYS = frozenset(
    {"SSL_CERT_FILE", "REQUESTS_CA_BUNDLE", AGENT_CONFIG_OVERLAY_ENV, AGENT_BIRTH_CONFIG_ENV}
)


# OS-canonical keys the delivery builders apply by mechanism, declared once for
# the A1 inventory: PATH + VIRTUAL_ENV are REBUILT by the venv activation at the
# delivery site (session_env / agent_launch — a login shell's profile would
# otherwise drop them, and forwarding a launchd/cron PATH would degrade daemon
# PATH), while TMPDIR / TEMP / TMP are copied non-empty into every child dict by
# `child_env` below (a child without TMPDIR falls back to the OS default temp
# root and any tool that pins a boot file to $TMPDIR drifts).
_OS_CANONICAL_KEYS = frozenset({"PATH", "VIRTUAL_ENV"})
_TEMP_DIR_KEYS = frozenset({"TMPDIR", "TEMP", "TMP"})

_PASSTHROUGH_ROWS = (
    _HOST_PASSTHROUGH_ROWS
    + _NETWORK_PROXY_ROWS
    + tuple(EnvField(key) for key in _GUIDE_PASSTHROUGH_KEYS | {REDIS_PASSWORD_ENV})
    + tuple(EnvField(key) for key in _OS_CANONICAL_KEYS | _TEMP_DIR_KEYS)
)

# ── Health ports: service -> env-var alias (a derived view of Settings) ──

# The health-port services (one Settings field each: `<svc>_health_port`,
# alias `AVA_<SVC>_HEALTH_PORT`, scope=host). Adding a daemon with a health
# port = one line here + the field in base/config/services.py; every
# consumer (derive_env, daemon.health, start, dotenv_boot's force set) follows
# automatically.
_HEALTH_PORT_SERVICES: tuple[str, ...] = (
    "labeler",
    "heartbeat",
    "task_maintenance",
    "events_maintenance",
    # The supervised pg-backup scheduler has its own per-unit health endpoint.
    "pg_backup",
    "memory_indexer",
    "ops",
    "delivery_watchdog",
    "im_bridge",
    "page_server",
    "agent_host",
    "gateway_watchdog",
    "agent_runner_watchdog",
)


@lru_cache(maxsize=1)
def health_port_env_aliases() -> dict[str, str]:
    """`{service: env-var alias}` for every health-port service — the derive
    surface. Derived from the Settings field names (fail-fast KeyError if a
    declared service has no `<svc>_health_port` field), never hand-copied."""
    return {svc: FIELD_ALIASES[f"{svc}_health_port"] for svc in _HEALTH_PORT_SERVICES}


# ── Derived sets (pure functions of the registry, memoized on first use) ──

# Consumption-matrix declarations by FIELD NAME (alias renames follow; a missing
# field is a KeyError — fail-fast, not a silent empty set).
# Machine-identity fields: a unit's identity is a per-unit fact — it must come
# from the unit's own .env / $AVA_HOME files, never from a parent process env.
# The Redis executable selection is also home-owned: inheriting it would make
# a sibling unit start a different server merely because of its caller.
_IDENTITY_FIELDS = frozenset(
    {
        "machine_serve_gateway",
        "machine_serve_agent_runner",
        "machine_serve_observability_station",
        "machine_host",
        "machine_name",
        "machine_description",
        "gateway_url",
        "memory_remote",
        "redis_bin_dir",
    }
)
# The derive_env() surface (base/cluster/derive.py): the cluster-isolation
# keys a cluster subprocess strips from its inherited env so its own .env is
# authoritative.
_DERIVED_FIELDS = frozenset(
    {
        "cluster_secret",
        "gateway_port",
        "gateway_url",
        "gateway_health_url",
        "frontend_healthcheck_url",
        "app_port",
        "milvus_port",
        "milvus_uri",
        "memory_search_port",
        "memory_search_uri",
        "browser_cdp_port",
        "permissions_helper_port",
        "db_url",
        "redis_url",
        "redis_admin_password",
        "events_channel",
    }
)
ADMIN_DATA_PLANE_ALIASES = frozenset({"AVA_REDIS_ADMIN_PASSWORD", REDIS_PASSWORD_ENV})


def _scope_aliases(*scopes: str) -> frozenset[str]:
    """Env aliases of every field whose scope is one of `scopes`.

    Reads the boot-lite static index, not the live registry: `load_ava_env`
    runs these projections before Settings exists, and the registry build would
    drag pydantic + all sub-models into every boot (#3621; index-vs-registry
    equality locked by tests/base/test_config_lite_table.py)."""
    return frozenset(FIELD_ALIASES[name] for name, scope in FIELD_SCOPES.items() if scope in scopes)


@lru_cache(maxsize=1)
def cluster_scope_aliases() -> frozenset[str]:
    """Every cluster-scoped settings alias (cluster-pinned + cluster-default):
    what a gateway process pops from its env and what dotenv_boot's env
    authority force-or-drops (F-s4-4). A new cluster-scoped field lands here
    automatically."""
    return _scope_aliases("cluster-pinned", "cluster-default")


@lru_cache(maxsize=1)
def agent_runner_cluster_aliases() -> frozenset[str]:
    """Agent-runner capability cluster-scoped aliases — the env vars a gateway
    process drops from its os.environ (the gateway never reads them and
    /api/bootstrap serves them from the .env FILE). Host-scope agent-runner keys
    are deliberately NOT here: the gateway machine may also run agent daemons
    (single box), and host-scope keys have no bootstrap fetch source. Derived:
    capability=agent-runner AND cluster scope (validated against the gateway
    consumption matrix by cli/commands/lifecycle/tests/test_gateway_consumer_guard.py)."""
    return frozenset(
        FIELD_ALIASES[name]
        for name, scope in FIELD_SCOPES.items()
        if FIELD_CAPABILITIES[name] == "agent-runner"
        and scope in ("cluster-pinned", "cluster-default")
    )


@lru_cache(maxsize=1)
def env_identity_keys() -> frozenset[str]:
    """Machine-IDENTITY env keys — stripped from the inherited environment when
    spawning a cluster's subprocess (alongside `derived_env_keys()`), and
    enforced by base.host.env.dotenv_boot._enforce_cluster_env_authority in EVERY
    process that loads a unit's .env: a key the unit's .env declares is forced
    from the file, an inherited one is dropped (the host-scoped gateway URL
    key stays env-suppliable — dotenv_boot's `_identity_env_only`)."""
    return frozenset(FIELD_ALIASES[n] for n in _IDENTITY_FIELDS)


@lru_cache(maxsize=1)
def derived_env_keys() -> frozenset[str]:
    """The exact set of env-var names `derive_env()` produces — the
    cluster-isolation keys. A subprocess started for a cluster strips these
    from the inherited environment so the cluster's own $AVA_HOME/.env is
    authoritative; otherwise the parent process (which imported base.config
    and thereby loaded its own prod .env into os.environ) would leak prod
    AVA_DB_URL / AVA_REDIS_URL into the child."""
    return (
        frozenset(FIELD_ALIASES[n] for n in _DERIVED_FIELDS)
        | frozenset(health_port_env_aliases().values())
        | {REDIS_PASSWORD_ENV}
    )


def _enabled_provider_key_envs() -> frozenset[str]:
    """The key variables declared by enabled provider plugins, loaded lazily.

    Provider keys deliberately have no Settings field: a provider plugin must
    be removable without widening core configuration. The binding contract is
    their sole declaration, and this helper is called only at env-delivery
    boundaries that need that declaration.
    """
    from base.lm import provider_api
    from base.lm.plugin_providers import ensure_provider_plugins_loaded

    ensure_provider_plugins_loaded()
    return frozenset(binding.key_env for binding in provider_api.REGISTRY.bindings.values())


@lru_cache(maxsize=1)
def session_forward_keys() -> frozenset[str]:
    """The daemon/session child allowlist (settings half) — the host-scope
    settings aliases (machine identity, per-unit health ports, the gateway URL
    the fetch dials), plus AVA_HOME, except the certification proof that only the
    agent-host finalizer receives through its targeted launcher projection. No
    cluster-scope value, no agent-scope knob, no non-modeled AVA_* identity
    (audit F-s3-4: the old denylist forwarded AVA_AGENT_ID into daemon
    sessions). The ambient passthroughs
    (DISPLAY/WAYLAND_DISPLAY/HOME/USER/LOGNAME) and the temp-dir vars are applied by
    `child_env`, not part of this set. A new host-scope field is forwarded
    automatically unless it is deliberately finalizer-only."""
    return (_scope_aliases("host") | {AVA_HOME_ENV}) - {MANIFEST_CERTIFICATION_SECRET_ENV}


@lru_cache(maxsize=1)
def launch_input_keys() -> frozenset[str]:
    """The env keys a root generation's launch digest binds, and nothing else.

    Declared launch inputs: the host-scope settings (AVA_HOME, machine
    identity, health ports, the admitted service PATH, the certification
    proof), PATH and VIRTUAL_ENV as the delivery rebuilds them from the
    runtime, and the finalizer ticket. The ambient host facts `child_env`
    copies from whoever launched it (display, HOME/USER/LOGNAME, temp dirs,
    proxy) are not: a service manager injects its own —
    systemd `User=` sets USER and LOGNAME, launchd adds TMPDIR — so an observer
    running with the fixed stage environment would name a different generation
    for the same launch. Positive list: a key a delivery adds later stays out
    of the digest until it is declared here.
    """
    return (
        _scope_aliases("host")
        | {AVA_HOME_ENV}
        | _OS_CANONICAL_KEYS
        | {MANIFEST_CERTIFICATION_FINALIZER_ENV}
    )


def manifest_certification_secret_env() -> dict[str, str]:
    """Return the proof's one-purpose projection for the agent-host finalizer.

    A non-finalizer config boot removes the proof from its ambient environment.
    The launcher re-reads only this value from its own unit file and supplies a
    one-use ticket, so the finalizer retains it while a child cannot obtain it
    merely by booting against that same file.
    """
    from base.host.env.dotenv_boot import manifest_certification_secret_from_env_file

    value = manifest_certification_secret_from_env_file()
    return (
        {
            MANIFEST_CERTIFICATION_SECRET_ENV: value,
            MANIFEST_CERTIFICATION_FINALIZER_ENV: "1",
        }
        if value
        else {}
    )


def _agent_guide_keys() -> frozenset[str]:
    return frozenset(FIELD_ALIASES[n] for n in _GUIDE_FIELDS) | _GUIDE_PASSTHROUGH_KEYS


@lru_cache(maxsize=1)
def agent_forward_keys() -> frozenset[str]:
    """The detached-agent child allowlist: the session set plus the agent-scope
    aliases (per-agent knobs: AVA_LLM_OVERRIDE etc.) plus the boot-time guide
    keys (cluster secret, TLS bundle, the
    overlay/birth JSON carriers — never argv, issue #974)."""
    return session_forward_keys() | _scope_aliases("agent") | _agent_guide_keys()


# ── Projections ──


def _require_role(role: ProcessRole) -> None:
    """Fail fast on a role outside the AVA_PROCESS_PROFILE vocabulary — the
    role axis of the env projections is that vocabulary, not a new enum."""
    if role not in ("gateway", "agent", "runner"):
        raise ValueError(
            f"unknown process role {role!r} — must be one of gateway/agent/runner "
            f"(the AVA_PROCESS_PROFILE vocabulary)"
        )


def _ensure_validated() -> None:
    """A1: a passthrough row whose key is also a Settings alias is a duplicate
    declaration — two homes for one fact, the drift class this registry kills.
    Runs once on first projection use (the row declarations are module
    constants, but the Settings-alias side needs the registry build)."""
    if getattr(_ensure_validated, "_done", False):
        return
    aliases = set(FIELD_ALIASES.values())
    for row in _PASSTHROUGH_ROWS:
        if row.key in aliases:
            raise RuntimeError(
                f"env key {row.key!r} is declared BOTH as a Settings field alias and as a "
                f"passthrough row in base/host/env/registry.py — register it exactly once"
            )
    _ensure_validated._done = True  # type: ignore[attr-defined]


def child_env(role: ProcessRole) -> dict[str, str]:
    """The parent->child env dict a `role` child receives (forwarding view).

    POSITIVE allowlist, not a drop list (Task #856 Phase C, audit F-s3-4): a
    non-modeled knob (AVA_AGENT_ID, ...) or agent-scope override never rides a
    daemon session. `role` reuses AVA_PROCESS_PROFILE: `gateway` and `runner`
    (daemon/session children — daemons belong to those profiles) get the
    session view; `agent` adds the agent-scope knobs and boot-time guide keys.
    Host passthroughs (DISPLAY/WAYLAND_DISPLAY/HOME/USER/LOGNAME), the temp-dir vars and the
    machine's network proxy configuration (NETWORK_PROXY_KEYS — the one egress
    channel a build child needs; issue #2095) are carried non-empty only — an
    empty $DISPLAY means "no display".

    The allow/drop decision is the DATA in this registry; the callers
    (base.sessions.env_forwarding / ops.agent_launch) are the mechanism that applies it.
    """
    _require_role(role)
    _ensure_validated()
    keys = (
        agent_forward_keys() if role == "agent" else session_forward_keys()
    )  # gateway / runner — the daemon/session view
    env = {k: os.environ[k] for k in keys if k in os.environ}
    if role == "agent":
        # Provider keys are non-modeled secrets. Only an agent process may need
        # them at build time; daemon/session children retain the narrower view.
        env.update(
            {key: os.environ[key] for key in _enabled_provider_key_envs() if key in os.environ}
        )
    for key in HOST_PASSTHROUGH_KEYS | _TEMP_DIR_KEYS | NETWORK_PROXY_KEYS:
        if os.environ.get(key):
            env[key] = os.environ[key]
    return env


def env_keep_set(role: ProcessRole) -> frozenset[str]:
    """The env-authority force families: keys whose unit-.env declaration must
    win over a polluted parent environment (cluster-scope aliases + machine
    identity). Role-independent today (every process enforces cluster env
    authority); the parameter exists for the day a profile needs a different
    keep set. dotenv_boot's per-unit force exemptions (health ports, the
    gateway URL series, the cluster secret) are its own, applied on top."""
    _require_role(role)
    _ensure_validated()
    return cluster_scope_aliases() | env_identity_keys()


def env_authority_drop_set(role: ProcessRole) -> frozenset[str]:
    """The env-authority drop families: keys removed from os.environ when the
    unit's .env does not declare them (cluster-scope aliases + machine
    identity). The gateway process additionally drops every agent-runner
    cluster-scope alias UNCONDITIONALLY (it never reads them; /api/bootstrap
    serves them from the .env FILE) — dotenv_boot applies that pop separately
    via `agent_runner_cluster_aliases`."""
    _require_role(role)
    _ensure_validated()
    return cluster_scope_aliases() | env_identity_keys()
