"""The env-key registry and its projections (R2 design, convergence point A).

Every env key this system forwards, forces, drops, or seeds is declared here or
in the Settings class metadata — except removable provider keys, whose enabled
plugin binding is their declaration:

- **Settings fields** declare themselves in `shared/config/<domain>.py`
  (`json_schema_extra` metadata); `shared/config_registry.py` builds the flat
  field registry from class metadata only (no Settings instantiation), and
  every projection below is a pure function of it — a new cluster-scoped field
  is force/dropped by the env-authority pass, forwarded to sessions, and
  distributed via /api/bootstrap with no hand-written set edit (the "env
  allowlist six-gap" incident class is structurally impossible: A3).
- **Non-Settings keys** (ambient display vars, Windows system keys, the
  overlay/birth JSON carriers, temp-dir vars, ...) are registered as
  `EnvField` passthrough rows below — one row per key (A1: exactly one
  declaration; a row whose key is also a Settings alias fails fast).

The old hand-written snapshots (`shared/env_keys.py`, 12 definitions) are gone.
The authority for which projection a key lands in is the **consumption matrix**
(which process kind actually reads the key — the per-projection declarations
below); capability/scope metadata only validates (deriving env sets from
capability was the 2026-08-06 #1570 P0).

Projections (the design's boundary currency):
- `child_env(role, platform)` — the parent->child forwarding view
  (SESSION/AGENT_FORWARD/HOST_PASSTHROUGH/WINDOWS_SYSTEM semantics);
  `role` reuses `AVA_PROCESS_PROFILE` (gateway/agent/runner) — daemons belong
  to the gateway/runner profiles.
- `env_keep_set(role)` / `env_authority_drop_set(role)` — the dotenv_boot
  env-authority force/drop families (set membership queries, not env dicts).
- `seed_allowlist()` — the install-time file-copy whitelist.

The derived sets are computed lazily on first use (memoized), not at module
level: computing them walks the Settings class metadata, which imports the
`shared.config` package (its __init__ constructs the Settings singleton), and
this module must stay importable before Settings exists — `dotenv_boot`'s
env-authority pass runs at `.env`-load time. First use happens during the very
import of `shared.config`, so the design's "collections are derived at import"
still holds; the mechanism just tolerates the boot-time import order.

POSIX delivery is the backend env-dict handoff (`shared.session_env.forward_env_dict`);
Windows delivers a dict. KEY=VALUE argv delivery stays forbidden (secrets never ride
argv — decisions/2026-07-30-secrets-never-ride-argv.md).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from typing import Literal

from shared.config_registry import (
    _fields,
    _schema_extra,
    field_alias,
    field_alias_map,
)
from shared.port_block import BLOCK_MAX, BLOCK_SIZE, PORT_OFFSETS

# The process-profile roles `child_env` / the authority projections accept —
# the existing AVA_PROCESS_PROFILE vocabulary (shared/config/profiles.py), not
# a new enum.
ProcessRole = Literal["gateway", "agent", "runner"]

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
# HOME and `gh auth status` flipped to "not logged in"). Forwarded non-empty
# only: an empty $DISPLAY means "no display", and forwarding "" would make a
# stripped display falsely look present in the child.
_HOST_PASSTHROUGH_ROWS = tuple(EnvField(key) for key in ("DISPLAY", "WAYLAND_DISPLAY", "HOME"))
HOST_PASSTHROUGH_KEYS = frozenset(row.key for row in _HOST_PASSTHROUGH_ROWS)

# Windows system keys a child env MUST carry (Task #945 follow-up): on Windows a
# child env dict is a wholesale replacement (CreateProcess / winproc — no login
# shell rebuilds it), so without these a child dies in winsock init before its
# first import (`import _overlapped`, WinError 10106 — v0.1.34 win daemons,
# 2026-08-07 agent spawn). Copied from os.environ, non-empty only; POSIX needs
# none of them (login shell rebuilds). Single declaration — the old parallel
# copy in shared/session_env.py is gone.
_WINDOWS_SYSTEM_ROWS = tuple(
    EnvField(key)
    for key in (
        "SYSTEMROOT",
        "WINDIR",
        "COMSPEC",
        "PATHEXT",
        "OS",
        "NUMBER_OF_PROCESSORS",
        "PROCESSOR_ARCHITECTURE",
        "USERPROFILE",
        "USERNAME",  # getpass.getuser() on Windows reads USERNAME and falls back
        # to `import pwd` when absent — which does not exist on Windows (Task #963:
        # every `ava start` converge crashed at the watchdog-probe schtasks
        # registration in allowlist-built children, three rollouts in a row).
        "USERDOMAIN",
        "HOMEDRIVE",
        "HOMEPATH",
        "APPDATA",
        "LOCALAPPDATA",
    )
)
WINDOWS_SYSTEM_ENV_KEYS = frozenset(row.key for row in _WINDOWS_SYSTEM_ROWS)

# The per-agent config overlay / birth stamp travel in env vars as JSON (never
# argv — issue #974: argv is world-readable via `ps`). Not Settings fields:
# they are process-bound carriers the exec launcher writes and child boot consumes.
AGENT_CONFIG_OVERLAY_ENV = "AVA_AGENT_CONFIG_OVERLAY"
AGENT_BIRTH_CONFIG_ENV = "AVA_AGENT_BIRTH_CONFIG"
# The Redis ACL runtime password is a file-only data-plane credential like the
# runner database password. derive_env emits it and process isolation strips it.
REDIS_PASSWORD_ENV = "AVA_REDIS_PASSWORD"  # noqa: S105 — env key, not a credential

# Bootstrap/identity guide keys an agent that self-fetches its config still
# needs forwarded before Settings: the home to resolve before Settings; the
# gateway URL to reach /api/bootstrap; the data-plane URL as a boot-time
# fallback; the TLS bundle for the fetch on corp-MITM hosts. Plus the two JSON
# carriers above. The Settings aliases are declared by field name so an alias
# rename follows automatically; the rest are passthrough rows.
_GUIDE_FIELDS = frozenset({"ava_home", "cluster_secret", "gateway_url", "gateway_port"})
_GUIDE_PASSTHROUGH_KEYS = frozenset(
    {"SSL_CERT_FILE", "REQUESTS_CA_BUNDLE", AGENT_CONFIG_OVERLAY_ENV, AGENT_BIRTH_CONFIG_ENV}
)

# Deprecated alias of AVA_GATEWAY_URL (still resolved via AliasChoices until its
# scheduled removal): the env-authority pass must strip a leaked prod value of
# EITHER spelling, so the alias is a registered row even though it is not a
# field alias.
AVA_PRIMARY_GATEWAY_URL = "AVA_PRIMARY_GATEWAY_URL"

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
    + _WINDOWS_SYSTEM_ROWS
    + tuple(
        EnvField(key)
        for key in _GUIDE_PASSTHROUGH_KEYS | {AVA_PRIMARY_GATEWAY_URL, REDIS_PASSWORD_ENV}
    )
    + tuple(EnvField(key) for key in _OS_CANONICAL_KEYS | _TEMP_DIR_KEYS)
)

# ── Health ports: service -> env-var alias (a derived view of Settings) ──

# The health-port services (one Settings field each: `<svc>_health_port`,
# alias `AVA_<SVC>_HEALTH_PORT`, scope=host). Adding a daemon with a health
# port = one line here + the field in shared/config/services.py; every
# consumer (derive_env, daemon_health, enroll, port_preflight, dotenv_boot's
# force set) follows automatically.
_HEALTH_PORT_SERVICES: tuple[str, ...] = (
    "labeler",
    "heartbeat",
    "task_maintenance",
    "events_maintenance",
    # The supervised pg-backup scheduler has its own per-unit health endpoint.
    "pg_backup",
    "pitr_uploader",
    "pitr_base_backup",
    "memory_indexer",
    "ops",
    # Added in the S4 isolation pass (F-s4-2): these two daemons predate the
    # per-unit health-port model and kept static legacy ports (8110/8111),
    # outside PORT_OFFSETS — `--health-port-base` did not move them, so two
    # co-located units collided on the shared default. They now sit in the
    # block (offsets 16/17) like every other health daemon: enroll writes them,
    # preflight checks them, derive_env materializes them.
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
    return {svc: field_alias(f"{svc}_health_port") for svc in _HEALTH_PORT_SERVICES}


def health_port_env(base: int) -> dict[str, str]:
    """`{AVA_*_HEALTH_PORT: port}` for a unit whose port block starts at `base`.

    `base` is a **block** base — the same number a cluster's registry record
    carries as its `gateway` port — so each daemon lands at `base +
    PORT_OFFSETS[svc]`, exactly where an allocated cluster would put it. Passing
    a block base rather than "the first health port" is what keeps a hand-set
    unit comparable to `ava cluster ls` output, and it means the ports stay
    inside one reserved port-block window instead of drifting across whatever a
    second convention would produce.

    Raises:
        ValueError: a derived port would fall outside the usable range. The
            operator states this fact; a base that cannot produce a legal block
            is a typo, and silently clamping it would bind ports nobody asked
            for.
    """
    aliases = health_port_env_aliases()
    ports = {var: base + PORT_OFFSETS[svc] for svc, var in aliases.items()}
    out_of_range = sorted(p for p in ports.values() if not (1024 <= p <= 65535))
    if out_of_range:
        raise ValueError(
            f"health-port base {base} derives ports outside 1024-65535: {out_of_range} "
            f"(a base is a {BLOCK_SIZE}-port block base; the highest offset in use is "
            f"{max(PORT_OFFSETS[svc] for svc in aliases)})"
        )
    return {var: str(port) for var, port in ports.items()}


# The lowest base a real unit port block can sit on. The legacy shared segment
# (LEGACY_AVA_PORTS, 8000-8120) ALSO satisfies offset-consistency by accident —
# prod's fixed pins (restarter 8102 / labeler 8103 / memory_indexer 8105 / ops
# 8106) line up with PORT_OFFSETS 3/4/6/7 onto one fake "base" (8099) — so
# consistency alone cannot tell a legacy unit from a block unit. Every real
# block base is operator-chosen or allocated at >= BLOCK_START (18000),
# hand-pinned enroll bases included (win: 18114, WSL2 default: 20027), so a
# solved base below this floor is a legacy pin sequence, never a block.
_HEALTH_PORT_BLOCK_FLOOR = 15000


def backfill_missing_health_ports(existing: dict[str, str]) -> dict[str, str]:
    """The `AVA_*_HEALTH_PORT` keys a block-style unit is missing, derived from
    the block its present keys already prove.

    A unit enrolled before a service joined `_HEALTH_PORT_SERVICES` carries an
    older key set in its `.env` (agent_host joined 2026-08-20, the capability
    watchdogs later), so that service's daemon falls back to the LEGACY shared
    port (`daemon_health.health_port`) — and on a mirrored localhost namespace
    (a Windows unit + its WSL2 sibling) two co-located units then collide on
    the same shared default (2026-09-02: win and wsl both fell back to 8114).
    The unit's own block base is recoverable from the keys it DOES have: each
    value equals `base + PORT_OFFSETS[svc]`.

    Returns {} unless at least two present keys solve to ONE common base at or
    above `_HEALTH_PORT_BLOCK_FLOOR` — the majority base. Keys that solve to a
    different base, or to none at all (unparseable value, or a base below the
    floor), are outliers and ignored: a single drifted key must not block the
    backfill for a block the remaining keys prove (2026-09-02: wsl carried one
    hand-set outlier, AVA_AGENT_RUNNER_WATCHDOG_HEALTH_PORT=20024, that aborted
    the whole table). A legacy unit's fixed 8102-8111 pins solve to different
    bases (their slot order predates PORT_OFFSETS) or to a base below the
    floor, and no two of them agree above it, so it is never misread as a
    block. Fewer than two agreeing keys cannot prove a block (one legacy pin
    is trivially "consistent"). A tie between two candidate bases is ambiguous
    and yields {} — guessing would bind ports nobody asked for. Keys already
    present are never rewritten — this heals ABSENCE, never drift (a hand-set
    emergency port, even one on the wrong slot, is the operator's to move
    through the config surface).
    """
    if len(existing) < 2:
        return {}
    aliases = health_port_env_aliases()
    svc_by_var = {var: svc for svc, var in aliases.items()}
    counts: dict[int, int] = {}
    for var, value in existing.items():
        svc = svc_by_var.get(var)
        if svc is None:
            continue
        try:
            base = int(value) - PORT_OFFSETS[svc]
        except (TypeError, ValueError):
            continue  # outlier: unparseable value
        if base < _HEALTH_PORT_BLOCK_FLOOR:
            continue  # outlier: legacy pin sequence, not a block
        counts[base] = counts.get(base, 0) + 1
    if not counts:
        return {}
    solved, n = max(counts.items(), key=lambda kv: (kv[1], kv[0]))
    if n < 2 or any(cnt == n and base != solved for base, cnt in counts.items()):
        return {}
    highest = solved + max(PORT_OFFSETS.values())
    if highest > 65535:
        return {}
    return {
        var: str(solved + PORT_OFFSETS[svc]) for svc, var in aliases.items() if var not in existing
    }


# The base `ava enroll` applies to a WSL2 host when --health-port-base is
# omitted (issue #1152). WSL2 shares its physical machine's localhost namespace
# with any co-located native Windows unit, and both otherwise fall back to the
# SAME hardcoded shared default (`shared.daemon_health.DEFAULT_PORTS`, the
# legacy 8102-8111 segment) — so a from-scratch WSL2 enroll with no flag would
# recreate the exact 2026-07-26 collision
# (decisions/2026-07-31-a-health-port-belongs-to-a-unit.md).
# This is a FIXED constant, not a scan: one slot past the birth allocator's own
# grid (`shared.port_block.BLOCK_START..BLOCK_MAX`), so a cluster later birthed
# on this same box can never claim it by allocation.
WSL_DEFAULT_HEALTH_PORT_BASE = BLOCK_MAX + BLOCK_SIZE

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
# The derive_env() surface (shared/cluster/derive.py): the cluster-isolation
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
        "db_admin_password",
        "redis_admin_password",
        "events_channel",
    }
)
ADMIN_DATA_PLANE_ALIASES = frozenset(
    {"AVA_DB_ADMIN_PASSWORD", "AVA_REDIS_ADMIN_PASSWORD", REDIS_PASSWORD_ENV}
)


def _scope_aliases(*scopes: str) -> frozenset[str]:
    """Env aliases of every field whose scope is one of `scopes`."""
    return frozenset(
        field_alias(name)
        for name, ref in _fields().items()
        if _schema_extra(ref.info).get("scope") in scopes
    )


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
    consumption matrix by tests/shared/test_gateway_consumer_guard.py)."""
    return frozenset(
        field_alias(name)
        for name, ref in _fields().items()
        if ref.capability == "agent-runner"
        and _schema_extra(ref.info).get("scope") in ("cluster-pinned", "cluster-default")
    )


@lru_cache(maxsize=1)
def env_identity_keys() -> frozenset[str]:
    """Machine-IDENTITY env keys — stripped from the inherited environment when
    spawning a cluster's subprocess (alongside `derived_env_keys()`), and
    enforced by shared.dotenv_boot._enforce_cluster_env_authority in EVERY
    process that loads a unit's .env: a key the unit's .env declares is forced
    from the file, an inherited one is dropped (the host-scoped gateway URL
    keys stay env-suppliable — dotenv_boot's `_identity_env_only`)."""
    return frozenset(field_alias(n) for n in _IDENTITY_FIELDS) | {AVA_PRIMARY_GATEWAY_URL}


@lru_cache(maxsize=1)
def derived_env_keys() -> frozenset[str]:
    """The exact set of env-var names `derive_env()` produces — the
    cluster-isolation keys. A subprocess started for a cluster strips these
    from the inherited environment so the cluster's own $AVA_HOME/.env is
    authoritative; otherwise the parent process (which imported shared.config
    and thereby loaded its own prod .env into os.environ) would leak prod
    AVA_DB_URL / AVA_REDIS_URL into the child."""
    return (
        frozenset(field_alias(n) for n in _DERIVED_FIELDS)
        | frozenset(health_port_env_aliases().values())
        | {REDIS_PASSWORD_ENV}
    )


@lru_cache(maxsize=1)
def seed_allowlist() -> frozenset[str]:
    """Convenience-seed allowlist — the ONLY keys the install-time seed step
    (`install.sh --worktree` / `python -m cli.install_cluster --seed-only`) may
    copy from the prod home's `.env` into a fresh worktree cluster's `.env`.
    Capability credentials only: LLM provider keys + web search/fetch keys, plus
    the one non-secret a key is useless without (`AVA_DASHSCOPE_BASE_URL` — a
    dedicated Model Studio workspace mints its key for its own host). Derived
    from the `seed: True` field marker (declared once on each such field in
    shared/config/lm.py + web.py), plus each enabled provider binding's key when
    it is either unmodeled or already seedable as a Settings field. A removable
    provider is therefore seedable without becoming a Settings field, but cannot
    turn an unrelated modeled setting into a seed credential. The runner database
    password is also explicitly excluded. Structurally disjoint from
    derived_env_keys() | env_identity_keys() (guarded by
    tests/shared/test_cluster_env.py): a seeded worktree must never inherit
    prod's data-plane identity or its cluster secret (always freshly minted),
    and never a singleton credential like AVA_TELEGRAM_BOT_TOKEN."""
    seed_keys = frozenset(
        field_alias(name)
        for name, ref in _fields().items()
        if _schema_extra(ref.info).get("seed") is True
    )
    settings_aliases = frozenset(field_alias(name) for name in _fields())
    from shared.cluster.derive import RUNNER_DB_PASSWORD_ENV

    plugin_seed_keys = (
        _enabled_provider_key_envs()
        - (settings_aliases - seed_keys)
        - derived_env_keys()
        - env_identity_keys()
        - {RUNNER_DB_PASSWORD_ENV}
    )
    return seed_keys | plugin_seed_keys


def _enabled_provider_key_envs() -> frozenset[str]:
    """The key variables declared by enabled provider plugins, loaded lazily.

    Provider keys deliberately have no Settings field: a provider plugin must
    be removable without widening core configuration. The binding contract is
    their sole declaration, and this helper is called only at env-delivery
    boundaries that need that declaration.
    """
    from shared.lm import provider_api
    from shared.lm._plugin_providers import ensure_provider_plugins_loaded

    ensure_provider_plugins_loaded()
    return frozenset(binding.key_env for binding in provider_api.REGISTRY.bindings.values())


@lru_cache(maxsize=1)
def session_forward_keys() -> frozenset[str]:
    """The daemon/session child allowlist (settings half) — the host-scope
    settings aliases (machine identity, per-unit health ports, the gateway URL
    the fetch dials, AVA_HOME). No cluster-scope value, no agent-scope knob, no
    non-modeled AVA_* identity (audit F-s3-4: the old denylist forwarded
    AVA_AGENT_ID into daemon sessions). The ambient passthroughs
    (DISPLAY/WAYLAND_DISPLAY/HOME) and the temp-dir vars are applied by
    `child_env`, not part of this set. A new host-scope field is forwarded
    automatically."""
    return _scope_aliases("host")


def _agent_guide_keys() -> frozenset[str]:
    return frozenset(field_alias(n) for n in _GUIDE_FIELDS) | _GUIDE_PASSTHROUGH_KEYS


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
    aliases = set(field_alias_map().values())
    for row in _PASSTHROUGH_ROWS:
        if row.key in aliases:
            raise RuntimeError(
                f"env key {row.key!r} is declared BOTH as a Settings field alias and as a "
                f"passthrough row in shared/env_registry.py — register it exactly once"
            )
    _ensure_validated._done = True  # type: ignore[attr-defined]


def child_env(role: ProcessRole, platform: str) -> dict[str, str]:
    """The parent->child env dict a `role` child receives (forwarding view).

    POSITIVE allowlist, not a drop list (Task #856 Phase C, audit F-s3-4): a
    non-modeled knob (AVA_AGENT_ID, ...) or agent-scope override never rides a
    daemon session. `role` reuses AVA_PROCESS_PROFILE: `gateway` and `runner`
    (daemon/session children — daemons belong to those profiles) get the
    session view; `agent` adds the agent-scope knobs and boot-time guide keys.
    `platform` ("posix" | "windows") selects the delivery semantics: on
    Windows the dict replaces the child env wholesale, so the system keys ride
    (non-empty); POSIX needs none of them (the child's login shell rebuilds).
    Host passthroughs (DISPLAY/WAYLAND_DISPLAY/HOME) and the temp-dir vars are
    carried non-empty only — an empty $DISPLAY means "no display".

    The allow/drop decision is the DATA in this registry; the callers
    (shared.session_env / ops.agent_launch) are the mechanism that applies it.
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
    for key in HOST_PASSTHROUGH_KEYS | _TEMP_DIR_KEYS:
        if os.environ.get(key):
            env[key] = os.environ[key]
    if platform == "windows":
        # UTF-8 mode for every Windows child interpreter (PEP 540): the
        # positive allowlist wholesale-replaces the child env, which drops the
        # PYTHONUTF8 seed ensure_utf8_stdio planted in the parent os.environ —
        # so daemons (agent-host) and their in-process hosted agents would
        # start on the legacy code page and crash printing CJK (task #2540,
        # win agent 2528). Inject it here for every role, the same value
        # exec-child bootstrap also requires.
        env["PYTHONUTF8"] = "1"
        for key in WINDOWS_SYSTEM_ENV_KEYS:
            if os.environ.get(key):
                env[key] = os.environ[key]
    elif platform != "posix":
        raise ValueError(f"child_env platform={platform!r} — must be 'posix' or 'windows'")
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
