"""Flat config-field registry — the EnvRegistry's Settings-field half.

The single place the registry is BUILT: walking the per-domain sub-model
classes (class metadata only — no Settings instantiation, so `dotenv_boot`,
which runs before Settings exists, can consume the registry's projections
through `base/host/env/registry.py` without a timing dependency).

It lives OUTSIDE the `base.config` package on purpose: importing any
`base.config.*` submodule executes `base/config/__init__.py`, which normally
constructs the Settings singleton (reads env / fetches from the gateway). The
settings-lite `AVA_CONFIG_FETCH=skip` mode defers that construction, and the
registry must stay importable before either path has a current value. The
sub-model class imports are therefore deferred into the build (first use), so
importing this module or `base/host/env/registry.py` never touches the package —
the build completes against a partially-initialized `base.config` when one is
already in flight (for example, the standalone `load_ava_env()` boot path), and
the double build that results is idempotent.

Settings fields and their `json_schema_extra` metadata are declared on the
owning domain sub-model. This registry exposes those declarations; non-Settings
keys are declared in `base/host/env/registry.py`. Consumer projections combine
scope/capability metadata with their explicit classification rules. Declaring a
field does not decide every projection automatically. See
`base/host/env/docs/registry.ava.okf.md` for the current projection boundaries.

Everything here is a pure function of the class declarations: building reads no
environment and constructs no Settings.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from functools import lru_cache
from types import UnionType
from typing import Any, Literal, Protocol, Union, cast, get_args, get_origin


class _FieldInfoLike(Protocol):
    """The pydantic ``FieldInfo`` surface this registry reads.

    Declared structurally rather than importing ``pydantic.fields``: this
    module must import without pydantic (it is importable before the config
    package), while the real FieldInfo objects only appear through the build.
    """

    alias: str | None
    serialization_alias: str | None
    json_schema_extra: dict[str, Any] | Callable[[dict[str, Any]], None] | None


# The capability a config field configures — the top-level config-panel section.
# `gateway` / `agent-runner` mirror the `MachineRole` capability tokens
# (`base.cluster.machine`); `common` is the third bucket for config owned by neither
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
DOMAIN_MODELS: tuple[tuple[str, str, str | type[Any], str], ...] = (
    ("lm", "LLM", "LmSettings", "agent-runner"),
    ("sandbox", "Sandbox", "SandboxSettings", "agent-runner"),
    ("agent", "Agent", "AgentSettings", "agent-runner"),
    ("web", "Web", "WebSettings", "agent-runner"),
    ("gateway", "Gateway", "GatewaySettings", "gateway"),
    ("daemon", "Daemon", "DaemonSettings", "gateway"),
    ("alerts", "Alerts", "AlertsSettings", "gateway"),
    ("data_plane", "Data plane", "DataPlaneSettings", "gateway"),
    ("services", "Services", "ServiceSettings", "gateway"),
    ("observability", "Observability", "ObservabilitySettings", "common"),
    ("display", "Display", "DisplaySettings", "common"),
    ("packages", "Packages", "PackagesSettings", "agent-runner"),
    ("feishu", "Feishu", "FeishuSettings", "gateway"),
    ("telegram", "Telegram", "TelegramSettings", "gateway"),
    ("walg", "WAL-G backup", "WalgSettings", "gateway"),
    ("general", "General", "GeneralSettings", "common"),
)

# The domain attribute names on `settings` — used by the profile fail-fast
# (Settings.__getattr__) to tell a real domain from a typo. Derived, so a new
# domain can never drift out of the fail-fast check.
DOMAIN_ATTRS = frozenset(attr for attr, _label, _model, _cap in DOMAIN_MODELS)

# The sub-model classes are imported INSIDE the build (see module docstring):
# importing them at module level would execute the `base.config` package while
# this registry is still initializing, breaking the dotenv_boot pre-Settings
# boot.
MODEL_CLASSES = {
    "LmSettings": "base.config.domains.lm",
    "SandboxSettings": "base.config.domains.sandbox",
    "AgentSettings": "base.config.domains.agent.settings",
    "WebSettings": "base.config.domains.web",
    "GatewaySettings": "base.config.domains.gateway",
    "DaemonSettings": "base.config.domains.daemon.settings",
    "AlertsSettings": "base.config.domains.observability.alerts",
    "DataPlaneSettings": "base.config.domains.storage.data_plane",
    "ServiceSettings": "base.config.domains.services.settings",
    "ObservabilitySettings": "base.config.domains.observability.settings",
    "DisplaySettings": "base.config.domains.display",
    "PackagesSettings": "base.config.domains.packages",
    "FeishuSettings": "base.config.domains.channels.feishu",
    "TelegramSettings": "base.config.domains.channels.telegram",
    "WalgSettings": "base.config.domains.storage.walg",
    "GeneralSettings": "base.config.domains.general",
}


@dataclass(frozen=True)
class _FieldRef:
    name: str
    domain: str  # attribute on `settings`, e.g. "lm"
    group: str  # frontend group label for the owning domain
    capability: Capability  # top-level config-panel section
    info: _FieldInfoLike


def schema_extra(info: _FieldInfoLike) -> dict[str, Any]:
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
# matrix, verified bidirectionally by cli/commands/lifecycle/tests/startup/test_gateway_consumer_guard.py).
# "schedule" / "all" / "" have no named-kind constraint ("schedule" runs without
# a profile and constructs every domain; "all" is satisfied by any consumer).
_RESTART_REQUIRED_PROFILE = {
    "agent": "agent",
    "gateway": "gateway",
    "ops": "runner",
}


def _validate_field(name: str, attr: str, extra: dict[str, Any], default_capability: str) -> str:
    """Fail fast on one field's ownership metadata; returns its validated capability."""
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
    _validate_scope_rules(name, extra, scope)
    _validate_lifecycle(name, attr, extra)
    _validate_restart_required(name, attr, extra)
    return capability


def _validate_scope_rules(name: str, extra: dict[str, Any], scope: object) -> None:
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
            f"cluster-default field {name!r} must be per_agent=True (the per-agent override gate)"
        )


def _validate_lifecycle(name: str, attr: str, extra: dict[str, Any]) -> None:
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


def _validate_restart_required(name: str, attr: str, extra: dict[str, Any]) -> None:
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
    # bidirectionally by cli/commands/lifecycle/tests/startup/test_gateway_consumer_guard.py). A
    # field only a gateway daemon reads marked "agent" would have the
    # operator restart the wrong process and the change silently not take
    # effect (the telegram/feishu/im_* 11-field bug this check seals, lost
    # in the main rebuild and re-landed by #1226). "schedule" / "all" / ""
    # carry no constraint.
    profile_kind = _RESTART_REQUIRED_PROFILE.get(restart_required)
    if profile_kind is not None:
        from base.config.profiles import PROCESS_PROFILES

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


@lru_cache(maxsize=1)
def _build_registry() -> dict[str, _FieldRef]:
    """Walk the sub-models into a flat name->owner registry, failing fast on an
    ownership-metadata mistake. The structural checks that used to live in a
    standalone `lint_config_scope` hook run here — a field with a bad or missing
    `scope` (which drives BOOTSTRAP_FIELDS + write routing) can never load, so
    the drift the lint guarded against is impossible rather than merely flagged.
    """
    from importlib import import_module

    from base.config.base import EnvSettings

    reg: dict[str, _FieldRef] = {}
    for attr, label, model_name, default_capability in DOMAIN_MODELS:
        if isinstance(model_name, str):
            # Deferred import (see module docstring): the sub-model classes live
            # under the `base.config` package. Importing them only at build time
            # keeps this registry importable before Settings construction.
            model = cast(
                "type[EnvSettings]",
                getattr(import_module(MODEL_CLASSES[model_name]), model_name),
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
            extra = schema_extra(info)
            capability = _validate_field(name, attr, extra, default_capability)
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


def fields() -> dict[str, _FieldRef]:
    return _build_registry()


# Leaf FieldInfo by name — the compat replacement for the old flat
# `Settings.model_fields` for code that iterated field metadata. Built lazily
# (PEP 562) — constructing it at module level would run `_build_registry()`
# during this module's import, whose deferred `base.config` package import
# re-enters this module mid-initialization (the package __init__ re-imports
# `field_alias` etc.) and blows up on a clean env (`ImportError: cannot import
# name 'field_alias' from partially initialized module ...` — Task #1099).
@lru_cache(maxsize=1)
def field_infos() -> dict[str, _FieldInfoLike]:
    return {n: r.info for n, r in fields().items()}


def __getattr__(name: str) -> Any:
    if name == "FIELD_INFOS":
        return field_infos()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def field_alias(name: str) -> str:
    """The `.env` / env-var alias a field reads from (serialization alias wins)."""
    info = fields()[name].info
    return info.serialization_alias or info.alias or name.upper()


def field_editor_type(annotation: object) -> tuple[str, list[str] | None]:
    """Return the config editor type and enum choices declared by ``annotation``.

    Optional fields retain the editor for their concrete member rather than
    degrading to free text. This is shared by the gateway metadata export and
    the settings-free local config CLI.
    """
    union_args = get_args(annotation)
    if get_origin(annotation) in (Union, UnionType) and type(None) in union_args:
        non_none = [item for item in union_args if item is not type(None)]
        if len(non_none) == 1:
            annotation = non_none[0]
    if annotation is bool:
        return "bool", None
    if annotation is int:
        return "int", None
    if annotation is float:
        return "float", None
    if get_origin(annotation) is Literal:
        return "enum", [str(item) for item in get_args(annotation)]
    return "string", None


def field_alias_map() -> dict[str, str]:
    """`{field name: env alias}` for every field — the flat map runtime_config /
    lint / session env-forwarding build on."""
    return {name: field_alias(name) for name in fields()}


def field_domain(name: str) -> str:
    """The `settings` attribute holding this field (e.g. 'lm')."""
    return fields()[name].domain


def field_names() -> set[str]:
    """Every leaf config field name across all sub-models."""
    return set(fields())


def per_agent_field_names() -> set[str]:
    """Leaf field names flagged `json_schema_extra={"per_agent": True}` — the
    framework fields a spawn/restart config overlay may override."""
    return {
        name for name, ref in fields().items() if schema_extra(ref.info).get("per_agent") is True
    }


def field_lifecycle(name: str) -> Lifecycle:
    """The per-agent lifecycle class of a `per_agent=True` field.

    Raises KeyError for a field that is not per-agent — that field has no
    per-agent instance, so it has no lifecycle (see the module docstring's
    boundary note).
    """
    extra = schema_extra(fields()[name].info)
    if extra.get("per_agent") is not True:
        raise KeyError(f"config field {name!r} is not per_agent — it has no lifecycle class")
    return cast(Lifecycle, extra["lifecycle"])


def frozen_field_names() -> set[str]:
    """Per-agent field names whose value is resolved once at spawn and replayed for
    the agent's life — the set `base/agents/birth_config.py` stamps into
    `agents_meta.birth_config`."""
    return {n for n in per_agent_field_names() if field_lifecycle(n) == "frozen"}


def live_field_names() -> set[str]:
    """Per-agent field names re-read from current cluster config at every process
    start (absent an explicit overlay). The complement of `frozen_field_names`."""
    return {n for n in per_agent_field_names() if field_lifecycle(n) == "live"}
