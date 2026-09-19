"""Unified runtime config — per-domain sub-models aggregated into one `settings`.

pydantic-settings validates types at startup (format errors blow up immediately),
centralizes defaults in one place, and replaces scattered `os.environ.get()` with
attribute access.

The former flat `Settings` god object is split by owning domain into sub-models
(`LmSettings`, `AgentSettings`, `DataPlaneSettings`, …), each its own
`BaseSettings` that populates from the flat `os.environ` through its fields' env
aliases. `Settings` aggregates one instance of each; access is nested
(`settings.lm.llm_model`). The split is invisible to `.env`: every AVA_* alias is
unchanged, so `.env` files, `_enforce_cluster_env_authority`, the bootstrap
payload, and the config PUT keep working byte-for-byte.

Precedence: env var > Field default. `$AVA_HOME/.env` is the single source of
truth — the config panel and `ava config set` write edits straight into it
(`set_key` / `unset_key` by alias; see `shared/runtime_config.py`), and
pydantic-settings reads it at startup. There is no separate override layer: a
value lives in exactly one place.

## Boot-lite boot (task #3621)

`import shared.config` does NOT construct `Settings`. It prepares the boot-lite
state — the `.env` load, the config-source decision, fail-fast validation of the
boot-path fields, the cluster clock — and serves reads from the generated static
index (`shared/config_lite_table.py`) through `settings`. The first touch of
anything outside that surface upgrades the process to the eager chain
(`shared/config/_lite.py` holds the state machine, `shared/config/_full.py` the
assembly) exactly once; overlay writes made before the upgrade are replayed onto
the singleton. `AVA_CONFIG_BOOT=eager` (read at import) restores the eager boot,
and `ensure_eager()` is the explicit entry point for processes that must
construct every domain at boot. Settings-lite maintenance verbs
(`AVA_CONFIG_FETCH=skip`) keep their deferred load — a broken `.env` stays
repairable.

Third-party-library-consumed secrets (ANTHROPIC_API_KEY, ...) are modeled
as fields — our own Python code accesses via `settings.<domain>.X.get_secret_value()`;
the LangChain SDK still reads `os.environ` itself (we do not prevent it).

The metadata machinery (`get_config_metadata`, `BOOTSTRAP_FIELDS`,
`bootstrap_config_values`, …) walks the sub-models and keys everything by the flat
field NAME — field names stay globally unique across sub-models, so the wire /
`.env` / bootstrap surfaces are unchanged. The frontend's config-panel display
grouping is NOT this metadata: the top-level display sections are the frontend's
own static regrouping (`ui/web/src/app/control/_config_groups.ts`); the second
level is the owning sub-model's `group` label. `capability` below is conceptual
ownership + the remote-view field filter, not panel grouping (default capability
per domain in `_DOMAIN_MODELS`). Each Field's json_schema_extra carries the
remaining metadata the frontend and distribution logic need:
- restart_required: "agent" | "ops" | "gateway" | "all" | "schedule" | "" — which process must restart after a change; "gateway" names the gateway process AND every gateway-profile daemon that consumes the field (im_bridge / memory_indexer / memory_search / milvus / ... — an `ava restart` bounces all of them); "schedule" = the gateway-hosted schedule runner
- writable: whether the frontend allows editing
- sensitive: whether the frontend masks the display
- scope: cluster-pinned | cluster-default | host | agent — drives BOOTSTRAP_FIELDS + write routing
- capability (optional, else the domain default): gateway | agent-runner | common —
  conceptual ownership + remote-view filter (agent-runner views show only agent-runner
  + common fields), orthogonal to scope; NOT panel grouping. `common` = not owned by
  a single capability (cluster-wide policy or the shared host identity).
- per_agent: whether a spawn/restart `config_overlay` may override the field
- lifecycle: frozen | live — REQUIRED on every per_agent field (see below)

## The per-agent config lifecycle axis (`lifecycle`)

`per_agent=True` says a field CAN be overridden for one agent. `lifecycle` says
what happens to that field when NOBODY overrode it — i.e. how a running agent
tracks a later change to the cluster default:

- **frozen** — resolved ONCE at the spawn boundary from the then-current default,
  persisted on the agent's own row (`agents_meta.birth_config`), and replayed on
  every restart / respawn / resurrect / compact for the rest of that agent's life.
  Flipping the cluster default afterwards moves nobody who already exists. This is
  the agent's *identity material*: the brain (model / effort / thinking budget) and
  everything that shapes the system prompt. Compact rebuilds the system prompt from
  current config, so a live default here would silently swap a living agent's
  identity mid-life.
- **live** — re-read from current cluster config at every process start. A cluster
  edit reaches every agent on its next restart. This is the right class for
  operational knobs (compaction thresholds, stream timeouts, recall tuning): they
  tune the runtime around the agent, they do not define it.

An explicit `config_overlay` is ORTHOGONAL to this axis and always wins:
`config_overlay > birth_config > current default`. The two stores are deliberately
separate so provenance survives — "the user chose this for this agent" and "this
was merely the cluster default the day it was born" must stay distinguishable.

**Boundary**: `lifecycle` applies only to `per_agent=True` fields. A field that is
not per-agent has no per-agent instance to freeze — cluster-scope config is by
definition read live by whatever process next starts, so declaring `lifecycle` on
one is a category error and the registry rejects it. Plugin `Config` fields
(`shared/plugin_config_registry.py`) are outside this registry and are not part of
the frozen set; they behave as `live` and only an explicit overlay pins them.

Resolution + stamping mechanics: `shared/birth_config.py`.

## Reading per-agent fields from turn-scoped code

Turn-scoped code (`agent/`, `ava/`, `ava_builtins/`, `shared/lm/`) reads
`per_agent` fields through the per-turn view — `turn_settings.<domain>.<field>`
(`shared/config/turn_view.py`) — never the bare singleton. The view resolves
the agent's contextvar-bound pins while the singleton holds the cluster
default. Outside an agent turn the view reads that live default.
Enforced by `scripts/lint_turn_scoped_config.py`.
"""

from __future__ import annotations

import os
import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, cast
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from shared.bootstrap import (
    CONFIG_FETCH_ENV as CONFIG_FETCH_ENV,
)
from shared.bootstrap import (
    CONFIG_FETCH_SKIP as CONFIG_FETCH_SKIP,
)
from shared.bootstrap import (
    config_source_is_local as config_source_is_local,
)
from shared.bootstrap import (
    should_fetch_from_gateway as should_fetch_from_gateway,
)
from shared.config import _lite
from shared.config._lite import (
    ConfigBuildWaitTimeoutError as ConfigBuildWaitTimeoutError,
)
from shared.config._lite import (
    ensure_eager as ensure_eager,
)
from shared.config._lite import (
    get_field as get_field,
)
from shared.config._lite import (
    set_field as set_field,
)
from shared.config.profiles import (
    AVA_PROCESS_PROFILE_ENV as AVA_PROCESS_PROFILE_ENV,
)
from shared.config.profiles import (
    PROCESS_PROFILES as PROCESS_PROFILES,
)
from shared.config.profiles import (
    PROFILE_UNSET as PROFILE_UNSET,
)
from shared.config.profiles import (
    ProcessProfile as ProcessProfile,
)
from shared.config.turn_view import (
    bind_agent_config as bind_agent_config,
)
from shared.config.turn_view import (
    turn_settings as turn_settings,
)
from shared.config_lite_table import (
    FIELD_ALIASES,
    FIELD_DOMAINS,
    PER_AGENT_FIELDS,
)
from shared.config_registry import (
    _DOMAIN_ATTRS as _DOMAIN_ATTRS,
)
from shared.config_registry import (
    _DOMAIN_MODELS as _DOMAIN_MODELS,
)
from shared.config_registry import (
    Capability as Capability,
)
from shared.config_registry import (
    Lifecycle as Lifecycle,
)
from shared.config_registry import (
    _schema_extra as _schema_extra,
)
from shared.dotenv_boot import (
    load_ava_env as load_ava_env,
)
from shared.netutil import (
    is_loopback_host as is_loopback_host,  # re-export: tests use config.is_loopback_host
)
from shared.url_secret import (
    url_with_host as url_with_host,  # re-export: tests use config.url_with_host
)

if TYPE_CHECKING:
    # Heavy dependencies used ONLY as types: the aggregate and the per-domain
    # sub-models live in shared/config/_full.py (importing them would construct
    # the eager chain this module defers). See _TYPE_CHECKING_ALLOWED in
    # scripts/lint_code_structure.py. None of these imports run at runtime.
    from shared.config._full import (
        DataPlaneSettings as DataPlaneSettings,
    )
    from shared.config._full import (
        Settings as Settings,
    )

    # Names the lite latch installs into this module's globals at upgrade
    # (`_full._facade_exports`) or serves settings-free in `__getattr__`:
    # declared so `from shared.config import X` / `from . import X` consumers
    # (`shared/config/editing.py`, the gateway config router, ops_config, tests)
    # keep resolving statically after the split.
    from shared.config.metadata import (
        CONFIG_UNCHANGED_SENTINEL as CONFIG_UNCHANGED_SENTINEL,
    )
    from shared.config.metadata import (
        ConfigFieldMeta as ConfigFieldMeta,
    )
    from shared.config.metadata import (
        env_override_values as env_override_values,
    )
    from shared.config.metadata import (
        get_config_metadata as get_config_metadata,
    )
    from shared.config.service_read import (
        bootstrap_config_values as bootstrap_config_values,
    )


# ── The static index surface (no registry build) ──
#
# The wire / .env / bootstrap surfaces are keyed by the flat field NAME. These
# five accessors answer from the generated index (shared/config_lite_table.py,
# regenerated by scripts/gen_config_lite_table.py; the drift test locks every
# entry equal to the live registry), so reflective callers — the per-agent
# overlay resolution, the health-port maps, the config CLI — work without
# building the registry.


def field_alias(name: str) -> str:
    """The `.env` / env-var alias a field reads from (serialization alias wins)."""
    return FIELD_ALIASES[name]


def field_alias_map() -> dict[str, str]:
    """`{field name: env alias}` for every field — the flat map runtime_config /
    lint / session env-forwarding build on."""
    return dict(FIELD_ALIASES)


def field_domain(name: str) -> str:
    """The `settings` attribute holding this field (e.g. 'lm')."""
    return FIELD_DOMAINS[name]


def field_names() -> set[str]:
    """Every leaf config field name across all sub-models."""
    return set(FIELD_DOMAINS)


def per_agent_field_names() -> set[str]:
    """Leaf field names flagged `json_schema_extra={"per_agent": True}` — the
    framework fields a spawn/restart config overlay may override."""
    return set(PER_AGENT_FIELDS)


# ── Cluster timezone — one clock for the whole cluster (Task #1758) ──
#
# User ruling 2026-08-27: the timezone is a cluster-level setting; every agent
# runner pulls it from the gateway and must NOT fall back to its own machine's
# OS timezone (a WSL runner whose OS zone was never switched exposed the
# mismatch). ``AVA_TIMEZONE`` (scope ``cluster-pinned``) already travels
# gateway -> runner: a pure agent-runner fetches it from ``GET /api/bootstrap``
# at every process start and the gateway unit keeps it in its own ``.env``, so
# a process that has the value in its environment holds the *authoritative*
# cluster timezone. The two helpers below are the one place that turns that
# value into a wall clock. The lite path resolves the value through the
# ``settings`` view (the boot-path index carries the timezone fields).


def cluster_tz_name() -> str | None:
    """The authoritative cluster timezone name, or ``None`` when this process
    holds none.

    Authoritative means ``settings.general.timezone`` was explicitly set at
    boot (env / unit ``.env`` / bootstrap fetch), not the silent
    ``America/Los_Angeles`` field default. ``None`` is the *host-zone
    fallback signal*: display paths render machine-local
    (``dt.astimezone(None)``), which is the documented degradation of a
    maintenance verb running while the gateway is down. This is the single
    authority check — callers must not re-implement the probe.
    """
    if not _lite.field_explicitly_set("timezone"):
        return None
    return settings.general.timezone


def host_tz_name() -> str:
    """This host's IANA timezone name, for paths that need an explicit name
    but have no authoritative cluster timezone (settings-lite cron).

    Resolves the ``/etc/localtime`` symlink (POSIX); falls back to ``UTC``
    where there is none (Windows) or the link is not a zoneinfo path. The
    name is only ever used as a wall-clock display zone for a lite process —
    the cluster clock is authoritative whenever it exists.
    """
    try:
        target = os.path.realpath("/etc/localtime")
    except OSError:
        return "UTC"
    marker = "/zoneinfo/"
    if marker in target:
        return target.split(marker, 1)[1]
    return "UTC"


def cluster_tz() -> ZoneInfo | None:
    """The cluster's timezone as a ``ZoneInfo``, or ``None`` when this process
    holds no authoritative ``AVA_TIMEZONE`` (settings-lite / bare checkout).

    ``None`` is the *host-zone fallback signal*: ``dt.astimezone(None)`` is
    machine-local, which is the documented degradation of a maintenance verb
    running while the gateway is down. A value that fails to parse as IANA
    (belt and braces — Settings already fails fast on it at construction)
    also yields ``None`` rather than crashing a display path.
    """
    name = cluster_tz_name()
    if name is None:
        return None
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return None


def _tzset() -> None:
    """Re-read the process TZ from ``os.environ["TZ"]`` where supported.

    ``time.tzset`` is POSIX-only (Windows CPython reads the OS zone directly
    and has no such function); the indirection exists so the Windows branch
    is exercised in tests without mutating the ``time`` module.
    """
    tzset = getattr(time, "tzset", None)
    if tzset is not None:
        tzset()


def apply_cluster_timezone() -> None:
    """Apply the cluster timezone to this process's wall clock (POSIX).

    Sets ``os.environ["TZ"]`` and calls ``time.tzset()`` when the process
    holds an authoritative ``AVA_TIMEZONE`` (the gateway unit's ``.env``, a
    runner's bootstrap fetch, a schedule runner's pinned spawn env). After
    this, every naive local-time read — ``datetime.now()``, no-arg
    ``.astimezone()``, loguru's ``{time}`` stamp, ``time.localtime()``, and
    children inheriting the env — follows the cluster clock. Before this
    hook existed, those reads used the host's OS zone, so a runner whose OS
    zone differs from the cluster's rendered machine-local wall clocks in
    logs and displays (2026-08-27 WSL mismatch, Task #1758).

    A process WITHOUT an authoritative value (settings-lite maintenance
    verbs, a bare checkout, CI) is left untouched: there is no cluster clock
    to apply, and forcing the field default ``America/Los_Angeles`` onto it
    would be wrong.

    No-op on Windows beyond exporting ``TZ`` for children: ``tzset`` does not
    exist there, and the explicit ``cluster_tz()`` reads cover the display
    paths instead.
    """
    name = cluster_tz_name()
    if name is None:
        return
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return
    os.environ["TZ"] = name
    _tzset()


def format_timestamp(dt: datetime) -> str:
    """Render a TZ-aware datetime as the agent-facing timestamp string.

    Format: ``[YYYY-MM-DD HH:MM:SS]``, e.g. ``[2026-05-06 14:32:05]``. When
    ``settings.general.message_timestamp_weekday`` is enabled the weekday
    abbreviated name is included between date and time. `dt` is converted to
    ``settings.general.timezone`` (default ``America/Los_Angeles``) first, so
    values read back from the database (TIMESTAMPTZ / UTC) render in the same
    wall clock as current-time stamps.

    No timezone suffix: ``settings.general.timezone`` is cluster-pinned, so the
    suffix was a constant string repeated on every timestamp — and an ambiguous
    one (``%Z`` gives ``PDT``/``PST`` across a DST boundary, and ``CST`` names
    two different zones). The agent is told the timezone once instead, by the
    standing context note in `agent/graph/_context_notes.py`.

    This is the single agent-facing timestamp representation: every producer
    goes through here, so a format change can never apply to some of an agent's
    timestamps and not others.
    """
    local = dt.astimezone(ZoneInfo(settings.general.timezone))
    if settings.general.message_timestamp_weekday:
        return local.strftime("[%Y-%m-%d %a %H:%M:%S]")
    return local.strftime("[%Y-%m-%d %H:%M:%S]")


def now_timestamp() -> str:
    """Return the current time as an agent-facing timestamp string.

    Thin wrapper over `format_timestamp`; see there for the format.
    """
    return format_timestamp(datetime.now(UTC))


# ── The stable `settings` object ──
#
# The view is lite until the first eager touch and then delegates to the
# constructed singleton; the object identity never changes, so every
# `from shared.config import settings` binding stays valid across the upgrade.
settings = cast("Settings", _lite.settings_view())


def _settings_instance() -> Any:
    """The constructed Settings singleton — building the eager chain (once) if
    this process is still lite. Asking for the real object means wanting the
    eager chain; the boot-lite runtime replays any pending overlay writes."""
    return _lite.upgrade("_settings_instance()")


def refresh_data_plane_settings() -> None:
    """Rebuild the data-plane sub-model from the current environment.

    Boot-lite shim (task #3621): the name stays importable without building the
    eager chain (settings-lite repair modules import it at module scope);
    calling it upgrades first and then runs the real implementation, which the
    upgrade installs over this name."""
    _lite.upgrade("refresh_data_plane_settings()")
    globals()["refresh_data_plane_settings"]()


# Private acceptance/test hook: the boot-lite state machine snapshot
# ({mode, reason, upgrades, pending_count, prepared}).
_boot_state = _lite.boot_state


def __getattr__(name: str) -> Any:
    """Eager-only names (Settings, BOOTSTRAP_FIELDS, the metadata /
    service-read / turn-view re-exports) are reached by upgrading once and
    re-reading this module's namespace (PEP 562, the `ava/__init__` latch
    shape).

    The two registry-backed field faces (`_FIELDS`, `FIELD_INFOS`) are served
    WITHOUT the upgrade: building the registry is construction-free — it
    never loads `.env` or constructs Settings — and the settings-lite repair
    paths (`ava config set` → `validate_env_patch_for_write`) import them in
    exactly the states where a broken `.env` makes Settings unable to
    construct (task #3621) — the pre-boot-lite import-time registry served
    the same surfaces. First access is not free: it imports the registry
    stack (~220 modules including `pydantic_settings`), while the boot state
    stays lite and the upgrade counter does not move."""
    if name.startswith("__") or _lite.is_upgrading():
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    if name in ("_FIELDS", "FIELD_INFOS"):
        from shared.config_registry import _field_infos, _fields

        return _fields() if name == "_FIELDS" else _field_infos()
    if name in ("CONFIG_UNCHANGED_SENTINEL", "ConfigFieldMeta"):
        # The write-path policy (`shared/config/editing.py`) imports these at
        # module level; both are settings-free metadata objects, so serve them
        # without the upgrade for the same repair-path reason as the field
        # faces above. Direct submodule import — `from shared.config import
        # metadata` would re-enter this `__getattr__` and upgrade.
        import importlib

        _metadata = importlib.import_module(f"{__name__}.metadata")
        return getattr(_metadata, name)
    _lite.upgrade(f"module attribute {name!r}")
    try:
        return globals()[name]
    except KeyError:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}") from None


# ── The boot tail ──
#
# Prepare the boot-lite state (or go eager now with `AVA_CONFIG_BOOT=eager`).
# A settings-lite process (`AVA_CONFIG_FETCH=skip`) degrades to the deferred
# load: nothing happens until the first config read.
_lite.boot()
