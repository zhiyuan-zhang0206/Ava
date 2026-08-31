"""Flat config-field registry — the EnvRegistry's Settings-field half.

The single place the registry is BUILT: walking the per-domain sub-model
classes (class metadata only — no Settings instantiation, so `dotenv_boot`,
which runs before Settings exists, can consume the registry's projections
through `shared/env_registry.py` without a timing dependency).

It lives OUTSIDE the `shared.config` package on purpose: importing any
`shared.config.*` submodule executes `shared/config/__init__.py`, which
constructs the Settings singleton (reads env / fetches from the gateway), and
the registry must stay importable before Settings exists. The sub-model class
imports are therefore deferred into the build (first use), so importing this
module or `shared/env_registry.py` never touches the package — the build
completes against a partially-initialized `shared.config` when one is already
in flight (the standalone `load_ava_env()` boot path, e.g.
and the double build that results is idempotent.

**Unique declaration point** (R2 design, convergence point A): a new env key is
declared exactly once —

- a Settings field: declare the field + its `json_schema_extra` metadata on the
  owning sub-model (`shared/config/<domain>.py`); the build picks it up and
  every projection in `shared/env_registry.py` updates automatically (the "env
  allowlist six-gap" class becomes structurally impossible — a new
  cluster-scoped field is force/dropped by the env-authority pass, forwarded to
  sessions, and distributed via /api/bootstrap without a single hand-written
  set edit);
- a non-Settings key (DISPLAY, Windows system keys, the overlay/birth JSON
  carriers, ...): add one passthrough row in `shared/env_registry.py`.

The authority for which projection a key lands in is the **consumption matrix**
(which process kind actually reads the key — declared per projection in
`shared/env_registry.py`); `capability` / `scope` metadata is validation, not
the derivation axis (deriving process env sets from capability was the
2026-08-06 #1570 P0).

Everything here is a pure function of the class declarations: building reads no
environment and constructs no Settings.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from typing import Any, Literal, cast

from pydantic.fields import FieldInfo

# The capability a config field configures — the top-level config-panel section.
# `gateway` / `agent-runner` mirror the `MachineRole` capability tokens
# (`shared.machine`); `common` is the third bucket for config owned by neither
# single capability (cluster-wide policy, or the host/carrier identity both
# capabilities on a box share). DISPLAY grouping only — orthogonal to `scope`,
# which alone drives distribution + write routing.
_ALLOWED_CAPABILITIES = frozenset({"gateway", "agent-runner", "common"})
# The resolved capability of a field — the top-level config-panel section.
Capability = Literal["gateway", "agent-runner", "common"]

# (domain attr, frontend group label, sub-model class, default capability).
# Order = field render order. The default capability owns every field in the
# domain unless a field overrides it with json_schema_extra={"capability": ...}
# (services / daemon / general straddle both capabilities — see those files).
_DOMAIN_MODELS: tuple[tuple[str, str, str | type[Any], str], ...] = (
    ("lm", "LLM", "LmSettings", "agent-runner"),
    ("sandbox", "Sandbox", "SandboxSettings", "agent-runner"),
    ("agent", "Agent", "AgentSettings", "agent-runner"),
    ("web", "Web", "WebSettings", "agent-runner"),
    ("gateway", "Gateway", "GatewaySettings", "gateway"),
    ("daemon", "Daemon", "DaemonSettings", "gateway"),
    ("alerts", "Alerts", "AlertsSettings", "gateway"),
    ("data_plane", "Data plane", "DataPlaneSettings", "gateway"),
    ("physical_backup", "Physical backup", "PhysicalBackupSettings", "gateway"),
    ("services", "Services", "ServiceSettings", "gateway"),
    ("observability", "Observability", "ObservabilitySettings", "common"),
    ("feishu", "Feishu", "FeishuSettings", "gateway"),
    ("telegram", "Telegram", "TelegramSettings", "gateway"),
    ("general", "General", "GeneralSettings", "common"),
)

# The domain attribute names on `settings` — used by the profile fail-fast
# (Settings.__getattr__) to tell a real domain from a typo. Derived, so a new
# domain can never drift out of the fail-fast check.
_DOMAIN_ATTRS = frozenset(attr for attr, _label, _model, _cap in _DOMAIN_MODELS)

# The sub-model classes are imported INSIDE the build (see module docstring):
# importing them at module level would execute the `shared.config` package
# __init__ (Settings construction) at registry-import time, breaking the
# dotenv_boot pre-Settings boot.
_MODEL_CLASSES = {
    "LmSettings": "shared.config.lm",
    "SandboxSettings": "shared.config.sandbox",
    "AgentSettings": "shared.config.agent",
    "WebSettings": "shared.config.web",
    "GatewaySettings": "shared.config.gateway",
    "DaemonSettings": "shared.config.daemon",
    "AlertsSettings": "shared.config.alerts",
    "DataPlaneSettings": "shared.config.data_plane",
    "PhysicalBackupSettings": "shared.config.physical_backup",
    "ServiceSettings": "shared.config.services",
    "ObservabilitySettings": "shared.config.observability",
    "FeishuSettings": "shared.config.feishu",
    "TelegramSettings": "shared.config.telegram",
    "GeneralSettings": "shared.config.general",
}


@dataclass(frozen=True)
class _FieldRef:
    name: str
    domain: str  # attribute on `settings`, e.g. "lm"
    group: str  # frontend group label for the owning domain
    capability: Capability  # top-level config-panel section
    info: FieldInfo


def _schema_extra(info: FieldInfo) -> dict[str, Any]:
    """The field's `json_schema_extra` as a plain dict (empty if unset / callable).

    pydantic types `json_schema_extra` as a `dict[str, JsonValue] | callable | None`
    union whose Unknown-keyed branch poisons every downstream `.get()`; funnel the
    isinstance narrowing through here so the metadata walkers read a clean dict.
    """
    extra = info.json_schema_extra
    return cast("dict[str, Any]", extra) if isinstance(extra, dict) else {}


_ALLOWED_SCOPES = frozenset({"cluster-pinned", "cluster-default", "host", "agent"})
# The per-agent config lifecycle classes (see the module docstring). Required on
# every per_agent field, forbidden on every other field.
_ALLOWED_LIFECYCLES = frozenset({"frozen", "live"})
# One per-agent field's lifecycle class: "frozen" (stamped into birth_config at
# spawn, replayed for life) or "live" (re-read from cluster config every start).
Lifecycle = Literal["frozen", "live"]

# Which process must restart after a change to the field — the operator's ONLY
# behavioral guide (the panel/CLI prompt from it; nothing restarts on its own).
# The empty string means no restart is needed. Every other value names a process
# kind that must CONSUME the field (see the profile cross-check in
# _build_registry); "schedule" is the gateway-hosted schedule runner, which has
# no config profile and reads every domain.
_ALLOWED_RESTART_REQUIRED = frozenset({"", "agent", "all", "gateway", "ops", "schedule"})
# restart_required -> the PROCESS_PROFILES set that must contain the field's
# domain for the value to make sense (the profile sets ARE the consumption
# matrix, verified bidirectionally by tests/shared/test_gateway_consumer_guard.py).
# "schedule" / "all" / "" have no named-kind constraint ("schedule" runs without
# a profile and constructs every domain; "all" is satisfied by any consumer).
_RESTART_REQUIRED_PROFILE = {
    "agent": "agent",
    "gateway": "gateway",
    "ops": "runner",
}


@lru_cache(maxsize=1)
def _build_registry() -> dict[str, _FieldRef]:
    """Walk the sub-models into a flat name->owner registry, failing fast on an
    ownership-metadata mistake. The structural checks that used to live in a
    standalone `lint_config_scope` hook run here — a field with a bad or missing
    `scope` (which drives BOOTSTRAP_FIELDS + write routing) can never load, so
    the drift the lint guarded against is impossible rather than merely flagged.
    """
    from importlib import import_module

    from shared.config._base import EnvSettings

    reg: dict[str, _FieldRef] = {}
    for attr, label, model_name, default_capability in _DOMAIN_MODELS:
        if isinstance(model_name, str):
            # Deferred import (see module docstring): the sub-model classes live
            # under the `shared.config` package, whose __init__ constructs the
            # Settings singleton — importing them here, at build time, is what
            # keeps the registry importable before Settings exists.
            model = cast(
                "type[EnvSettings]",
                getattr(import_module(_MODEL_CLASSES[model_name]), model_name),
            )
        else:
            # A test-injected synthetic model class (config_lifecycle tests).
            model = model_name
        for name, info in model.model_fields.items():
            if name in reg:
                raise RuntimeError(
                    f"config field name {name!r} is not unique across sub-models "
                    f"({reg[name].domain} vs {attr}) — flat keying (wire/.env/bootstrap) "
                    f"requires globally-unique field names"
                )
            extra = _schema_extra(info)
            scope = extra.get("scope")
            # The field's capability is its explicit override or the domain default;
            # a bad override can never load (same fail-fast posture as scope).
            capability = extra.get("capability", default_capability)
            if capability not in _ALLOWED_CAPABILITIES:
                raise RuntimeError(
                    f"config field {name!r} ({attr}) has capability={capability!r}; must be "
                    f"one of {sorted(_ALLOWED_CAPABILITIES)} — it names the config-panel "
                    f"section the field renders under"
                )
            if scope not in _ALLOWED_SCOPES:
                raise RuntimeError(
                    f"config field {name!r} ({attr}) has scope={scope!r}; must be one of "
                    f"{sorted(_ALLOWED_SCOPES)} — scope drives BOOTSTRAP_FIELDS distribution + "
                    f".env write routing"
                )
            if scope == "host" and not isinstance(extra.get("remote_writable"), bool):
                raise RuntimeError(
                    f"host-scope field {name!r} must declare remote_writable: bool "
                    f"(got {extra.get('remote_writable')!r})"
                )
            if scope != "host" and extra.get("remote_writable") is True:
                raise RuntimeError(
                    f"non-host field {name!r} sets remote_writable=True — only host-scope "
                    f"fields are remote-writable"
                )
            if scope == "cluster-default" and extra.get("per_agent") is not True:
                raise RuntimeError(
                    f"cluster-default field {name!r} must be per_agent=True (the per-agent "
                    f"override gate)"
                )
            lifecycle = extra.get("lifecycle")
            if extra.get("per_agent") is True:
                if lifecycle not in _ALLOWED_LIFECYCLES:
                    raise RuntimeError(
                        f"per-agent field {name!r} ({attr}) has lifecycle={lifecycle!r}; must be "
                        f"one of {sorted(_ALLOWED_LIFECYCLES)} — 'frozen' is stamped into "
                        f"agents_meta.birth_config at spawn and replayed for the agent's life, "
                        f"'live' is re-read from cluster config at every process start. There is "
                        f"no default: the choice is a semantic ruling about whether the field is "
                        f"the agent's identity material or an operational knob."
                    )
            elif lifecycle is not None:
                raise RuntimeError(
                    f"field {name!r} ({attr}) declares lifecycle={lifecycle!r} but is not "
                    f"per_agent=True — the lifecycle axis only applies to fields that HAVE a "
                    f"per-agent instance to freeze; cluster-scope config is read live by "
                    f"whatever process next starts"
                )
            restart_required = extra.get("restart_required", "")
            if restart_required not in _ALLOWED_RESTART_REQUIRED:
                raise RuntimeError(
                    f"config field {name!r} ({attr}) has restart_required={restart_required!r}; "
                    f"must be one of {sorted(_ALLOWED_RESTART_REQUIRED)} — it names the process "
                    f"the operator must restart after a change, and the panel/CLI prompt from it"
                )
            # Cross-check against the consumption matrix: restart_required names a
            # process kind, so that kind's config profile must contain the field's
            # domain (the profile sets ARE the consumption matrix — verified
            # bidirectionally by tests/shared/test_gateway_consumer_guard.py). A
            # field only a gateway daemon reads marked "agent" would have the
            # operator restart the wrong process and the change silently not take
            # effect (the telegram/feishu/im_* 11-field bug this check seals, lost
            # in the main rebuild and re-landed by #1226). "schedule" / "all" / ""
            # carry no constraint.
            profile_kind = _RESTART_REQUIRED_PROFILE.get(restart_required)
            if profile_kind is not None:
                from shared.config.profiles import PROCESS_PROFILES

                profile = PROCESS_PROFILES[profile_kind]  # type: ignore[index]
                if attr not in profile:
                    raise RuntimeError(
                        f"field {name!r} ({attr}) declares restart_required={restart_required!r} "
                        f"but its domain is not in the {profile_kind!r} process profile "
                        f"({sorted(profile)}) — the named process kind "
                        f"does not consume this field, so the operator would restart the wrong "
                        f"process and the change would silently not take effect. Point it at "
                        f"the kind that actually reads it (the im_bridge-consumed telegram/feishu/"
                        f"im_* fields are 'gateway'), or use 'all' or 'schedule'."
                    )
            reg[name] = _FieldRef(
                name=name,
                domain=attr,
                group=label,
                # Validated against _ALLOWED_CAPABILITIES just above (a bad value
                # already raised), so the narrowing to Capability is sound.
                capability=cast(Capability, capability),
                info=info,
            )
    return reg


def ensure_built() -> None:
    """Force the registry build (first call only; the build is memoized).

    `shared/config/__init__.py` calls this at its own import so its module-level
    consumers (BOOTSTRAP_FIELDS etc.) see a built registry regardless of import
    order; every other caller gets the build lazily on first use."""
    _build_registry()


def _fields() -> dict[str, _FieldRef]:
    return _build_registry()


# Leaf FieldInfo by name — the compat replacement for the old flat
# `Settings.model_fields` for code that iterated field metadata. Built lazily
# (PEP 562) — constructing it at module level would run `_build_registry()`
# during this module's import, whose deferred `shared.config` package import
# re-enters this module mid-initialization (the package __init__ re-imports
# `field_alias` etc.) and blows up on a clean env (`ImportError: cannot import
# name 'field_alias' from partially initialized module ...` — Task #1099).
@lru_cache(maxsize=1)
def _field_infos() -> dict[str, FieldInfo]:
    return {n: r.info for n, r in _fields().items()}


def __getattr__(name: str) -> Any:
    if name == "FIELD_INFOS":
        return _field_infos()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def field_alias(name: str) -> str:
    """The `.env` / env-var alias a field reads from (serialization alias wins)."""
    info = _fields()[name].info
    return info.serialization_alias or info.alias or name.upper()


def field_alias_map() -> dict[str, str]:
    """`{field name: env alias}` for every field — the flat map runtime_config /
    lint / session env-forwarding build on."""
    return {name: field_alias(name) for name in _fields()}


def field_domain(name: str) -> str:
    """The `settings` attribute holding this field (e.g. 'lm')."""
    return _fields()[name].domain


def field_names() -> set[str]:
    """Every leaf config field name across all sub-models."""
    return set(_fields())


def per_agent_field_names() -> set[str]:
    """Leaf field names flagged `json_schema_extra={"per_agent": True}` — the
    framework fields a spawn/restart config overlay may override."""
    return {
        name for name, ref in _fields().items() if _schema_extra(ref.info).get("per_agent") is True
    }


def field_lifecycle(name: str) -> Lifecycle:
    """The per-agent lifecycle class of a `per_agent=True` field.

    Raises KeyError for a field that is not per-agent — that field has no
    per-agent instance, so it has no lifecycle (see the module docstring's
    boundary note).
    """
    extra = _schema_extra(_fields()[name].info)
    if extra.get("per_agent") is not True:
        raise KeyError(f"config field {name!r} is not per_agent — it has no lifecycle class")
    return cast(Lifecycle, extra["lifecycle"])


def frozen_field_names() -> set[str]:
    """Per-agent field names whose value is resolved once at spawn and replayed for
    the agent's life — the set `shared/birth_config.py` stamps into
    `agents_meta.birth_config`."""
    return {n for n in per_agent_field_names() if field_lifecycle(n) == "frozen"}


def live_field_names() -> set[str]:
    """Per-agent field names re-read from current cluster config at every process
    start (absent an explicit overlay). The complement of `frozen_field_names`."""
    return {n for n in per_agent_field_names() if field_lifecycle(n) == "live"}
