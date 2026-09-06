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

Third-party-library-consumed secrets (ANTHROPIC_API_KEY, ...) are modeled
as fields — our own Python code accesses via `settings.<domain>.X.get_secret_value()`;
the LangChain SDK still reads `os.environ` itself (we do not prevent it).
`load_ava_env(~/.ava/.env)` runs during normal config import. Settings-lite
maintenance verbs set `AVA_CONFIG_FETCH=skip`, which defers it until the first
real `settings` read so a broken `.env` remains repairable.

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
from threading import RLock
from typing import Any, cast
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, Field

import shared.config_registry as _config_registry
from shared.bootstrap import (
    CONFIG_FETCH_ENV,
    CONFIG_FETCH_SKIP,
    config_source_is_local,
    should_fetch_from_gateway,
)
from shared.config.agent import AgentSettings
from shared.config.alerts import AlertsSettings
from shared.config.daemon import DaemonSettings
from shared.config.data_plane import (
    DataPlaneSettings,
)
from shared.config.data_plane import (
    _self_machine_host as _self_machine_host,  # re-export: service_read resolves it through this module so tests can monkeypatch it
)
from shared.config.feishu import FeishuSettings
from shared.config.gateway import GatewaySettings
from shared.config.general import GeneralSettings
from shared.config.lm import LmSettings
from shared.config.observability import ObservabilitySettings
from shared.config.physical_backup import PhysicalBackupSettings
from shared.config.profiles import (
    AVA_PROCESS_PROFILE_ENV,
    PROCESS_PROFILES,
    PROFILE_UNSET,
    ProcessProfile,
)
from shared.config.sandbox import SandboxSettings
from shared.config.service_read import (
    warn_deprecated_env_aliases as warn_deprecated_env_aliases,
)
from shared.config.services import ServiceSettings
from shared.config.telegram import TelegramSettings
from shared.config.web import WebSettings
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
from shared.config_registry import (
    field_alias as field_alias,
)
from shared.config_registry import (
    field_alias_map as field_alias_map,
)
from shared.config_registry import (
    field_domain as field_domain,
)
from shared.config_registry import (
    field_lifecycle as field_lifecycle,
)
from shared.config_registry import (
    field_names as field_names,
)
from shared.config_registry import (
    frozen_field_names as frozen_field_names,
)
from shared.config_registry import (
    live_field_names as live_field_names,
)
from shared.config_registry import (
    per_agent_field_names as per_agent_field_names,
)
from shared.dotenv_boot import load_ava_env
from shared.netutil import (
    is_loopback_host as is_loopback_host,  # re-export: tests use config.is_loopback_host
)
from shared.url_secret import (
    url_with_host as url_with_host,  # re-export: tests use config.url_with_host
)

# The flat field registry (shared/config_registry.py) is built eagerly here so
# its module-level consumers (BOOTSTRAP_FIELDS etc.) and re-exported names see
# every declared field before the normal Settings boot or its settings-lite defer.


# ── Flat field registry (name -> owning sub-model + FieldInfo) ──
#
# The wire / .env / bootstrap surfaces are keyed by the flat field NAME. Field
# names are globally unique across sub-models, so this registry lets every
# metadata consumer walk the decomposed model exactly as it walked the old flat
# one, and lets the per-agent config overlay resolve a flat key to its sub-model.
# The build itself lives in shared/config_registry.py (importable before
# Settings exists — the dotenv_boot env-authority pass runs at .env-load time).


_config_registry.ensure_built()
# The flat name->owner registry. The registry module builds lazily (the
# dotenv_boot pre-Settings boot imports it without triggering the package);
# the build was forced just above, so this binding is the real dict.
_FIELDS: dict[str, Any] = _config_registry._fields()

# Leaf FieldInfo by name — the compat replacement for the old flat
# `Settings.model_fields` for code that iterated field metadata. The registry
# module builds lazily; the build was forced above, so this binding is eager.
FIELD_INFOS: dict[str, Any] = {n: r.info for n, r in _FIELDS.items()}


class Settings(BaseModel):
    """Aggregate of the per-domain config sub-models. Access is nested:
    `settings.lm.llm_model`, `settings.data_plane.db_url`. Each sub-model is a
    `BaseSettings` that reads the flat env; this composite just holds one of each.

    `profile` selects the per-process domain set (PROCESS_PROFILES): a domain
    outside the profile is NOT constructed and its attribute access raises an
    actionable AttributeError (fail-fast — a cross-profile read used to
    silently read a default). `has_domain()` is the dynamic-code escape hatch.
    With no profile marker the composite constructs every domain, unchanged.
    """

    # The process profile this aggregate was constructed for (None = full
    # construction). A plain excluded field, not a PrivateAttr: the profile
    # fail-fast in __getattr__ reads it as a normal attribute, and
    # model_dump()/validation never sees it (exclude=True).
    profile: str | None = Field(default=None, exclude=True)

    lm: LmSettings = Field(default_factory=LmSettings)
    alerts: AlertsSettings = Field(default_factory=AlertsSettings)
    sandbox: SandboxSettings = Field(default_factory=SandboxSettings)
    agent: AgentSettings = Field(default_factory=AgentSettings)
    web: WebSettings = Field(default_factory=WebSettings)
    gateway: GatewaySettings = Field(default_factory=GatewaySettings)
    daemon: DaemonSettings = Field(default_factory=DaemonSettings)
    # DataPlaneSettings has required no-default fields (db_url / redis_url) that
    # BaseSettings fills from env at construction; pyright sees the zero-arg factory
    # as under-supplied.
    data_plane: DataPlaneSettings = Field(default_factory=DataPlaneSettings)  # pyright: ignore[reportArgumentType, reportUnknownVariableType]
    physical_backup: PhysicalBackupSettings = Field(default_factory=PhysicalBackupSettings)
    services: ServiceSettings = Field(default_factory=ServiceSettings)
    observability: ObservabilitySettings = Field(default_factory=ObservabilitySettings)
    feishu: FeishuSettings = Field(default_factory=FeishuSettings)
    telegram: TelegramSettings = Field(default_factory=TelegramSettings)
    general: GeneralSettings = Field(default_factory=GeneralSettings)

    def __init__(self, *, profile: str | None = PROFILE_UNSET, **data: Any) -> None:
        """Construct the aggregate for `profile`.

        `profile` defaults to the process's AVA_PROCESS_PROFILE env marker; an
        explicit `profile=None` builds every domain (config-service read paths,
        tests, CLI). An unknown profile name fails fast — the marker is set by
        launchers, not by hand.
        """
        if profile == PROFILE_UNSET:
            profile = os.environ.get(AVA_PROCESS_PROFILE_ENV)
        if profile is not None and profile not in PROCESS_PROFILES:
            raise ValueError(
                f"{AVA_PROCESS_PROFILE_ENV}={profile!r} is not a known process profile; "
                f"must be one of {sorted(PROCESS_PROFILES)} — the marker is set by the "
                f"process launcher, not by hand"
            )
        super().__init__(**data)
        self.profile = profile
        if profile is not None:
            allowed = PROCESS_PROFILES[profile]
            for attr, *_rest in _DOMAIN_MODELS:
                if attr not in allowed:
                    vars(self).pop(attr, None)

    def __getattr__(self, name: str) -> Any:
        # A missing attribute on this aggregate is either a typo or — on a
        # profile-limited instance — a domain the process profile deliberately
        # does not construct (fail-fast: a cross-profile read used to silently
        # read a default). The actionable message names the fix for both.
        profile = self.profile
        if (
            profile is not None
            and name in _DOMAIN_ATTRS
            and name not in PROCESS_PROFILES[cast("ProcessProfile", profile)]
        ):
            raise AttributeError(
                f"'{profile}' process profile does not construct the {name!r} config "
                f"domain (per-process config, Task #856) — nothing in this process "
                f"kind reads settings.{name}. If this read is legitimate, add the "
                f"domain to the '{profile}' profile in PROCESS_PROFILES AND to the "
                f"consumption-matrix guard (tests/shared/test_gateway_consumer_guard.py); "
                f"otherwise move the read to the process kind that owns the domain. "
                f"Dynamic code can check settings.has_domain({name!r}) first."
            ) from None
        raise AttributeError(f"'{type(self).__name__}' object has no attribute {name!r}")

    def has_domain(self, name: str) -> bool:
        """Whether this process's profile constructs the `name` config domain.

        The escape hatch for dynamic code (plugins) that must probe before
        reading; static code should simply access `settings.<domain>` and let
        the fail-fast AttributeError point at the fix.
        """
        profile = self.profile
        if profile is None:
            return True
        return name in PROCESS_PROFILES[cast("ProcessProfile", profile)]


# A settings-lite process's placeholder for the required redis URL (the db URL
# placeholder is UNANCHORED_DB_SENTINEL, which shared/db.connect already refuses
# with an actionable error). Lite verbs never dial the data plane, so the values
# are never reached; they exist only so Settings constructs.
_LITE_REDIS_URL = "redis://config-lite@127.0.0.1:1/0"


def _plant_lite_placeholders() -> None:
    """Plant never-dialed placeholders for the required data-plane URLs so
    Settings constructs without a gateway fetch. A value already in env/.env (the
    suite's pins, CI's sentinels, a stale pre-cutover materialization) is left
    alone — the placeholder only fills the nothing-at-all case."""
    from shared.dotenv_boot import UNANCHORED_DB_SENTINEL

    os.environ.setdefault("AVA_DB_URL", UNANCHORED_DB_SENTINEL)
    os.environ.setdefault("AVA_REDIS_URL", _LITE_REDIS_URL)


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
# value into a wall clock. They read ``os.environ`` rather than ``settings``
# deliberately: this module is still being built when ``apply_cluster_timezone``
# runs, and the env is the same single source the Settings field is built from.


def cluster_tz_name() -> str | None:
    """The authoritative cluster timezone name, or ``None`` when this process
    holds none.

    Authoritative means ``settings.general.timezone`` was explicitly set at
    Settings build (env / unit ``.env`` / bootstrap fetch), not the silent
    ``America/Los_Angeles`` field default. ``None`` is the *host-zone
    fallback signal*: display paths render machine-local
    (``dt.astimezone(None)``), which is the documented degradation of a
    maintenance verb running while the gateway is down. This is the single
    authority check — callers must not re-implement the
    ``model_fields_set`` probe.
    """
    if "timezone" not in settings.general.model_fields_set:
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


_settings_lock = RLock()


class _SettingsState:
    value: Settings | None = None


_settings_state = _SettingsState()


def _settings_instance() -> Settings:
    """Construct Settings on first use, leaving field inspection settings-free."""
    if _settings_state.value is not None:
        return _settings_state.value
    with _settings_lock:
        if _settings_state.value is not None:
            return _settings_state.value
        load_ava_env()
        if config_source_is_local():
            pass  # a gateway-capable unit: the local .env IS the source of truth
        elif os.environ.get(CONFIG_FETCH_ENV) == CONFIG_FETCH_SKIP:
            _plant_lite_placeholders()
        elif should_fetch_from_gateway():
            from shared.bootstrap import inject_config_from_gateway

            inject_config_from_gateway()
        else:
            _plant_lite_placeholders()
        _settings_state.value = Settings()
        # Apply the clock only after the singleton exists: cluster_tz_name()
        # reads it through the public facade below.
        apply_cluster_timezone()
        return _settings_state.value


class _SettingsProxy:
    """Resolve the established ``settings`` public object only when read."""

    def __getattr__(self, name: str) -> Any:
        return getattr(_settings_instance(), name)

    def __setattr__(self, name: str, value: Any) -> None:
        setattr(_settings_instance(), name, value)


# Imports throughout the project keep their public type and behavior. Settings-
# lite maintenance verbs defer construction; every normal config import retains
# the established fail-fast boot contract.
settings = cast("Settings", _SettingsProxy())

if os.environ.get(CONFIG_FETCH_ENV) != CONFIG_FETCH_SKIP:
    settings = _settings_instance()


def refresh_data_plane_settings() -> None:
    """Re-read the unit's `.env` and rebuild `settings.data_plane` in place.

    A long-lived process (the rollout orchestrator) builds its Settings
    singleton once at startup. When the work it drives rewrites `.env` — most
    notably a data-plane credential rotation migration run by the local leg's
    child `ava start` (the 2026-08-25 secret split) — the singleton still
    carries the pre-rotation values and every later data-plane write from this
    process fails with SASL authentication. On 2026-08-25 the pin advance, the
    compensating unpause and the update-lock release all died exactly that way,
    stranding the cluster paused with a stale pin while the watchdog
    force-checked-out the stale pin underneath the landed gateway.

    This re-runs the boot env load (the cluster-env authority pass refreshes
    the cluster-scope aliases from the new file) and rebuilds only the
    data-plane sub-model in place, so existing `from shared.config import
    settings` references see the fresh credentials without a process restart.
    Other domains are left untouched: the rotation is a data-plane fact, and a
    full singleton swap would surprise subsystems that cache a sub-model.
    """
    from shared.dotenv_boot import load_ava_env

    load_ava_env()
    settings.data_plane = DataPlaneSettings()  # pyright: ignore[reportCallIssue]


# Cluster-common config an agent-runner fetches from the gateway via
# GET /api/bootstrap. Derived from each field's ownership scope: the two cluster
# scopes are distributed; host / agent fields are not.
BOOTSTRAP_FIELDS: tuple[str, ...] = tuple(
    name
    for name, ref in _FIELDS.items()
    if _schema_extra(ref.info).get("scope") in ("cluster-pinned", "cluster-default")
    and _schema_extra(ref.info).get("bootstrap", True) is not False
)


def get_field(name: str) -> Any:
    """Current value of a leaf field by name, resolved to its owning sub-model.

    The escape hatch for reflective / dynamic access — a flat
    `getattr(settings, name)` no longer works now that fields live on
    `settings.<domain>`. Static access should use the nested attribute directly
    (`settings.lm.llm_model`); this is for call sites that hold the field name as a
    runtime string (health-port map, model-key map, capability probes)."""
    ref = _FIELDS[name]
    return getattr(getattr(settings, ref.domain), name)


def set_field(name: str, value: Any) -> None:
    """In-place set a field on its owning sub-model of the singleton. Every holder
    of `from shared.config import settings` sees it (same sub-model instance). Used
    by the per-agent config overlay at process boot."""
    ref = _FIELDS[name]
    setattr(getattr(settings, ref.domain), name, value)


def flat_dump(mode: str = "python") -> dict[str, Any]:
    """Flat `{field name: value}` dump across all sub-models — the shape the old
    flat `settings.model_dump()` produced, used by the config-overlay snapshot.

    Reads through the profile-independent path (`_all_domains_settings` for a
    domain the running profile excludes), so an agent process — whose profile
    excludes daemon/alerts/telegram/feishu — can still snapshot the full
    overlay base at bind time."""
    from shared.config.service_read import _all_domains_settings

    out: dict[str, Any] = {}
    for attr, _label, _model, _cap in _DOMAIN_MODELS:
        try:
            sub = getattr(settings, attr)
        except AttributeError:
            sub = getattr(_all_domains_settings(), attr)
        out.update(sub.model_dump(mode=mode))
    return out


# Cluster-common config an agent-runner fetches from the gateway via
# GET /api/bootstrap. Derived from each field's ownership scope: the two cluster
# scopes are distributed; host / agent fields are not.


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


# ── Re-exports of the split-out consumer modules (import sites unchanged) ──
#
# These import the config package lazily and load at the tail of this module
# (a top-level import would be circular), hence the E402 suppressions.
from shared.config.metadata import (  # noqa: E402
    CONFIG_UNCHANGED_SENTINEL as CONFIG_UNCHANGED_SENTINEL,
)
from shared.config.metadata import (  # noqa: E402
    ConfigFieldMeta as ConfigFieldMeta,
)
from shared.config.metadata import (  # noqa: E402
    env_override_values as env_override_values,
)
from shared.config.metadata import (  # noqa: E402
    get_config_metadata as get_config_metadata,
)
from shared.config.service_read import (  # noqa: E402
    bootstrap_config_values as bootstrap_config_values,
)
from shared.config.service_read import (  # noqa: E402
    current_field_values as current_field_values,
)
from shared.config.turn_view import (  # noqa: E402
    bind_agent_config as bind_agent_config,
)
from shared.config.turn_view import (  # noqa: E402
    resolve_agent_config_pins as resolve_agent_config_pins,
)
from shared.config.turn_view import (  # noqa: E402
    turn_settings as turn_settings,
)
