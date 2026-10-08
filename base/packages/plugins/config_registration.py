"""Plugin config — a declared Pydantic class bound once from its disk image, frozen, immutable.

Symmetric with whole-class state declaration (`agent/state.py`): a plugin writes a Pydantic BaseModel
and declares it (`PluginContributions.config`, from the `contribute()` of its `default_config.py` config
face, `base/packages/plugins/config_face.py`); the framework
handles namespace isolation + disk persistence. Differences:

- State is runtime mutable, written via LangGraph reducer; Config is a boot
  snapshot, frozen, immutable after instantiation.
- State field names ∈ BaseAgentState are shared with base; Config has an
  independent namespace per plugin.
- State persists to LangGraph checkpoint; Config persists to
  `~/.ava/configs/<plugin>/config.json` (full image, not partial overlay).

`ava.sdk_surface.install` binds each declared class through `bind_plugin_config(plugin, cls)`, which
reads `~/.ava/configs/<plugin>/config.json`, validates it against the class schema, instantiates it and
stores it for `ava.sdk_surface.settings.plugins.<plugin>` (the returned undo drops it on uninstall); a missing
image is written with the defaults first. Mismatch raises `SchemaDriftError`, guiding the user to run
`ava plugins update` (reconciles the disk image to the current schema — adds new defaults, drops
removed fields — fully automatic; also run by the `ava start` converge step). The install treats a
bind failure as a load failure of that plugin.

Field metadata `json_schema_extra={"per_agent": True}` marks "can be overridden
by per-agent CLI overlay".

Declaration (`ava_builtins/plugins/<name>/default_config.py`):

    from pydantic import BaseModel, ConfigDict, Field
    from base.packages.plugins.extensions import PluginContributions

    class MyConfig(BaseModel):
        model_config = ConfigDict(frozen=True)
        threshold: int = Field(default=100)
        marker: str = Field(default=".git", json_schema_extra={"per_agent": True})

    def contribute() -> PluginContributions:
        return PluginContributions(config=MyConfig)
"""

import json
import math
from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal, cast, overload

from pydantic import BaseModel, ValidationError
from pydantic.fields import FieldInfo

from base import paths
from base.host.env.agent_slices import AgentSlices


class PluginConfigError(Exception):
    """Root of plugin config register / bind failures. Plugin author / CLI use this for coarse catch."""


class DuplicateRegistration(PluginConfigError):  # noqa: N818
    """A plugin's Config is bound twice in one install — a second `bind_plugin_config` for the
    same plugin."""


class SchemaDriftError(PluginConfigError):
    """Disk image field set inconsistent with plugin Config class — run
    `ava plugins update` to auto-merge (write defaults for new fields, drop removed)."""


class InvalidConfigData(PluginConfigError):  # noqa: N818 — parent already has Error suffix, subclass describes specific scenario
    """Disk image JSON parse failure / Pydantic validation failure — someone
    edited a line by hand and broke it; framework does not silently repair,
    plugin author / user handles it."""


class InvalidConfigOverlay(PluginConfigError):  # noqa: N818
    """A config overlay failed validation; the message says which key and why.
    Nothing was spawned or queued — fix the dict and call again."""


# Two-layer dict, written only by `bind_plugin_config` (the installer):
#   _PLUGIN_CONFIG_CLASSES: plugin → Cls
#   _PLUGIN_CONFIGS:       plugin → instance (agent reads via `ava.sdk_surface.settings.plugins.<n>`)

_PLUGIN_CONFIG_CLASSES: dict[str, type[BaseModel]] = {}
_PLUGIN_CONFIGS: dict[str, BaseModel] = {}


def bind_plugin_config(plugin: str, cls: type[BaseModel]) -> Callable[[], None]:
    """Bind `plugin`'s declared Config class from its disk image; returns the undo.

    Reads the disk image (a missing one is written with the defaults), validates it against `cls`,
    instantiates it and stores both for the readers. Only the installer calls this.

    Raises:
        TypeError: cls is not a BaseModel subclass — typo (passed dataclass / plain class).
        DuplicateRegistration: this plugin already has a bound config.
        SchemaDriftError: disk image field set inconsistent with cls (plugin upgraded schema).
        InvalidConfigData: disk image JSON parse failure / Pydantic validation failure.
    """
    if not (isinstance(cls, type) and issubclass(cls, BaseModel)):
        raise TypeError(
            f"plugin {plugin!r} declared config {cls!r}, which is not a BaseModel subclass — "
            f"write `class FooConfig(BaseModel): ...` and declare it in `PluginContributions.config`."
        )
    if plugin in _PLUGIN_CONFIG_CLASSES:
        raise DuplicateRegistration(
            f"plugin {plugin!r} already has a bound config ({_PLUGIN_CONFIG_CLASSES[plugin].__name__})"
        )
    instance = _instantiate_from_disk(plugin, cls)
    _PLUGIN_CONFIG_CLASSES[plugin] = cls
    _PLUGIN_CONFIGS[plugin] = instance

    def undo() -> None:
        _PLUGIN_CONFIG_CLASSES.pop(plugin, None)
        _PLUGIN_CONFIGS.pop(plugin, None)

    return undo


def _instantiate_from_disk(plugin: str, cls: type[BaseModel]) -> BaseModel:
    """Read disk image (auto-write default if missing), validate schema, return instance."""
    config_path = disk_image_path(plugin)
    if not config_path.exists():
        write_default_disk_image(plugin, cls)
    return read_config_image(cls, config_path)


def read_config_image[C: BaseModel](cls: type[C], config_path: Path) -> C:
    """Read a plugin's full config image without binding or writing it.

    A missing image uses the declared defaults, including before first converge.
    An existing image must contain exactly the declared fields and validate;
    malformed data is never repaired or replaced with defaults.
    """
    try:
        content = config_path.read_text()
    except FileNotFoundError:
        return cls()

    try:
        disk_data = json.loads(content)
    except json.JSONDecodeError as e:
        raise InvalidConfigData(
            f"plugin config disk image JSON parse failed ({config_path}): {e}"
        ) from e

    if not isinstance(disk_data, dict):
        raise InvalidConfigData(
            "plugin config disk image top-level must be a JSON object, got "
            f"{type(disk_data).__name__} ({config_path})"
        )
    disk_data = cast("dict[str, Any]", disk_data)

    disk_keys = set(disk_data.keys())
    cls_keys = set(cls.model_fields.keys())
    if disk_keys != cls_keys:
        added = cls_keys - disk_keys
        removed = disk_keys - cls_keys
        raise SchemaDriftError(
            f"plugin config disk image schema drift ({config_path}): "
            f"added={sorted(added)} removed={sorted(removed)}. "
            f"Run `ava plugins update` to auto-merge."
        )

    try:
        return cls(**disk_data)
    except ValidationError as e:
        raise InvalidConfigData(
            f"plugin config disk image Pydantic validation failed ({config_path}): {e}"
        ) from e


def disk_image_path(plugin: str) -> Path:
    """Disk image path — `~/.ava/configs/<plugin>/config.json`. Does not pre-create directory."""
    return paths.ava_home() / "configs" / plugin / "config.json"


def write_default_disk_image(plugin: str, cls: type[BaseModel]) -> Path:
    """Serialize cls full default values to disk image (overwrite, create parent dir).

    Used for `ava start` first boot + `ava plugins update` writing initial
    image for new plugin. Existing field values are not preserved — caller
    should check path.exists() first.
    """
    instance = cls()  # All defaults; cls fields lacking default raise ValidationError
    config_path = disk_image_path(plugin)
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(instance.model_dump_json(indent=2) + "\n")
    return config_path


def merge_disk_image_schema(plugin: str, cls: type[BaseModel]) -> tuple[set[str], set[str]]:
    """Used by `ava plugins update` — auto-merge disk image and cls schema diff.

    Behavior:
    - New fields (in cls, not on disk): write cls default value to disk image
    - Removed fields (on disk, not in cls): dropped from the disk image so it
      converges to exactly the current schema's field set. This is what lets the
      strict-equality check in `_instantiate_from_disk` pass after an upgrade
      that removed a field — keeping the stale field would leave every agent
      spawn failing with SchemaDriftError.
    - Type changed (new cls field type incompatible with old disk value via Pydantic):
      raise InvalidConfigData, user must manually migrate
    - No diff: no-op, don't write disk

    Disk missing → write default for first-boot scenario, returns (all_fields, set()).

    Returns:
        (added, removed) — for CLI log display.

    Raises:
        InvalidConfigData: disk JSON malformed or post-merge Pydantic validation
            failure (type incompatibility).
    """
    config_path = disk_image_path(plugin)

    if not config_path.exists():
        write_default_disk_image(plugin, cls)
        return set(cls.model_fields.keys()), set()

    try:
        disk_data = json.loads(config_path.read_text())
    except json.JSONDecodeError as e:
        raise InvalidConfigData(
            f"plugin {plugin!r} disk image JSON malformed ({config_path}): {e}"
        ) from e

    disk_keys = set(disk_data.keys())
    cls_keys = set(cls.model_fields.keys())
    added = cls_keys - disk_keys
    removed = disk_keys - cls_keys

    if not added and not removed:
        return set(), set()

    default_dump = cls().model_dump(mode="json")
    # Rebuild the image from the current schema's field set: keep the disk value
    # where the field still exists, fill the cls default for a new field, and drop
    # any field the schema no longer declares. The image converges to exactly
    # cls_keys, so `_instantiate_from_disk`'s strict-equality check passes next boot.
    merged: dict[str, object] = {
        field: disk_data[field] if field in disk_keys else default_dump[field] for field in cls_keys
    }

    # Validate merged data — incompatible type blows up here (old disk value type
    # does not match new cls field definition).
    try:
        cls(**merged)
    except ValidationError as e:
        raise InvalidConfigData(
            f"plugin {plugin!r} disk image post-merge Pydantic validation failed "
            f"(type incompatible, related to plugin upgrade; manual migrate needed {config_path}): {e}"
        ) from e

    config_path.write_text(json.dumps(merged, indent=2, sort_keys=True) + "\n")
    return added, removed


@overload
def get_plugin_config(plugin: str, slices: AgentSlices) -> BaseModel: ...
@overload
def get_plugin_config[T: BaseModel](plugin: str, slices: AgentSlices, cls: type[T]) -> T: ...


def get_plugin_config(
    plugin: str, slices: AgentSlices, cls: type[BaseModel] | None = None
) -> BaseModel:
    """Read plugin config instance — already bound by the install — for the agent whose `slices` are given.

    Pass `cls` to let pyright narrow the return type (e.g. `get_plugin_config("ava_compact",
    slices, CompactConfig).auto_compact_tokens`). Runtime does not validate cls matches
    actual instance — caller's responsibility.

    Agent-scoped: the agent's `config_overlay` is layered over the bound disk image
    (`base/packages/plugins/config_view.py`); a hook reads `runtime.context.require_agent()`.

    Raises:
        KeyError: plugin declares no config or it is not installed yet — typo / wrong ordering.
    """
    _ = cls
    return slices.plugin_config(plugin)


def all_plugin_configs(slices: AgentSlices) -> dict[str, BaseModel]:
    """All bound plugin config instances for the agent whose `slices` are given. See
    `get_plugin_config` on scoping."""
    return slices.plugin_configs()


def process_plugin_config(plugin: str) -> BaseModel:
    """This process's own instance of `plugin`'s config — what boot's `apply_config_overlay`
    rebuilt with the process's agent overlay merged in (the exec child, a script)."""
    return _PLUGIN_CONFIGS[plugin]


def _schema_extra(info: FieldInfo) -> dict[str, Any]:
    """Collapse pydantic's `FieldInfo.json_schema_extra` (typed as
    dict | callable | None, its dict branch partially Unknown) to a plain
    `dict[str, Any]` for the per_agent metadata lookups. Mirrors
    `base/config/__init__.schema_extra`."""
    extra = info.json_schema_extra
    return cast("dict[str, Any]", extra) if isinstance(extra, dict) else {}


def _per_agent_fields_for(cls: type[BaseModel]) -> set[str]:
    """Set of field names with `json_schema_extra={"per_agent": True}` (on that cls)."""
    out: set[str] = set()
    for name, info in cls.model_fields.items():
        if _schema_extra(info).get("per_agent") is True:
            out.add(name)
    return out


def overlay_config_classes() -> dict[str, type[BaseModel]]:
    """The plugin config classes a per-agent overlay may name, by plugin.

    The class every ENABLED plugin declares in its `default_config.py` config face, read from disk — so
    the gateway and ops, which bind no plugin config, validate against the same classes as the agent
    process — with a class this process has bound taking precedence.
    """
    from base.packages.plugins import enable_config
    from base.packages.plugins.config_face import enabled_config_classes

    installed = enable_config.installed_plugin_dirs()
    config = enable_config.load_for_runtime(set(installed))
    enabled = {
        name: plugin_dir
        for name, plugin_dir in installed.items()
        if name in config.plugins and config.plugins[name].enabled
    }
    return {**enabled_config_classes(enabled), **_PLUGIN_CONFIG_CLASSES}


def resolve_overlay_targets(overlay: dict[str, object]) -> dict[str, tuple[str | None, str]]:
    """Dispatch overlay flat keys to framework Settings or a specific plugin Config.

    PR-E design: `ava.self.restart(config_overlay={"auto_compact_fraction": 0.7})` —
    flat dict, each key must **uniquely** match either a framework Settings
    field or a plugin Config field. Validates per_agent=True, uniqueness,
    field existence.

    Returns:
        dict[overlay_key] = (plugin_name_or_None, field_name). plugin=None for
        framework Settings fields; plugin=<name> for plugin fields.

    Raises:
        InvalidConfigOverlay: field does not exist / same name collision across
            framework+plugin / not per_agent. Detailed message marks key + reason.
    """
    # Deferred import to avoid module init cycle. `settings` is decomposed into
    # per-domain sub-models; the overlay keys are flat field names, so resolve
    # against the flat name set / per-agent set the config package exposes.
    from base.config import field_names, per_agent_field_names

    framework_per_agent = per_agent_field_names()
    framework_all = field_names()

    plugin_classes = overlay_config_classes()
    out: dict[str, tuple[str | None, str]] = {}
    for key in overlay:
        matches: list[tuple[str | None, str]] = []
        if key in framework_all:
            matches.append((None, key))
        for plugin, cls in plugin_classes.items():
            if key in cls.model_fields:
                matches.append((plugin, key))

        if not matches:
            raise InvalidConfigOverlay(
                f"overlay key {key!r} is not in framework Settings nor any plugin "
                f"Config — typo? Known plugins: {sorted(plugin_classes)}"
            )
        if len(matches) > 1:
            owners = ", ".join(f"{p or 'framework'}.{f}" for p, f in matches)
            raise InvalidConfigOverlay(
                f"overlay key {key!r} collides across multiple owners: {owners} — current PR-E "
                f"does not support explicit namespace; rename one or wait for namespace support."
            )
        plugin, field = matches[0]
        if plugin is None:
            if field not in framework_per_agent:
                raise InvalidConfigOverlay(
                    f"overlay key {key!r} is a framework Settings field but not marked "
                    f"per_agent=True — cluster-consistent fields (db_url etc.) do not allow "
                    f"per-agent override; if truly needed change framework Settings and plugin Config metadata."
                )
        else:
            cls = plugin_classes[plugin]
            if field not in _per_agent_fields_for(cls):
                raise InvalidConfigOverlay(
                    f"overlay key {key!r} is in plugin {plugin!r} but not marked "
                    f"per_agent=True — add json_schema_extra={{'per_agent': True}} to the "
                    f"default_config.py field to enable overlay."
                )
        out[key] = (plugin, field)
    return out


def _validate_model_membership(value: object) -> str | None:
    """A model-name overlay field must be a registered id (the model catalog,
    plugin-registered models included). An unregistered id would pass
    the Pydantic str type check, persist, and crash the next boot at model
    build (Task #1704 — the deepseek-v4-flash-vision incident)."""
    from base.lm.plugin_providers import model_catalog

    models = model_catalog().models
    if isinstance(value, str) and value in models:
        return None
    valid_models = ", ".join(sorted(models))
    return f"value {value!r} is not a registered model; valid models: {valid_models}"


def _validate_reasoning_effort_range(value: object) -> str | None:
    from base.lm.effort import EFFORT_VOCAB

    # None = unset (the field is `str | None` and a None overlay is a no-op) —
    # accepted, matching the pre-PR behavior.
    if value is None:
        return None
    valid_values = ("", *EFFORT_VOCAB)
    if isinstance(value, str) and value in valid_values:
        return None
    rendered_values = ", ".join(repr(candidate) for candidate in valid_values)
    return f"value {value!r} is not valid; valid values: {rendered_values}"


def _range_validator(
    *,
    gt: float | None = None,
    ge: float | None = None,
    le: float | None = None,
) -> Callable[[object], str | None]:
    """Numeric bound validator for overlay fields.

    - None passes (a `T | None` field's unset sentinel — the type layer
      already decided None is legal).
    - NaN / ±Inf are rejected outright: a NaN timeout never fires (hang), an
      Inf heartbeat limit defeats the cap, an Inf fraction is meaningless.
    - Then the bounds apply: ``gt`` (strictly greater), ``ge`` (at least),
      ``le`` (at most). All bounds inclusive-exclusive as named.

    Non-numeric values (bool included) fall through — the Pydantic type
    validation that runs before this step owns them.
    """

    def check(value: object) -> str | None:
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        if not math.isfinite(value):
            return f"value {value!r} must be a finite number (NaN/Inf rejected)"
        if gt is not None and value <= gt:
            return f"value {value!r} must be greater than {gt}"
        if ge is not None and value < ge:
            return f"value {value!r} must be at least {ge}"
        if le is not None and value > le:
            return f"value {value!r} must be at most {le}"
        return None

    return check


# Semantic range validators for framework overlay fields — Pydantic type
# validation (above) accepts any string / any number, so fields whose legal
# values form a named universe get an explicit range check here. Model-name
# fields check model-catalog membership (an unknown id would crash the next boot at
# model build); numeric fields get finite + bound checks (NaN/Inf/negative
# timeouts and out-of-window fractions would wedge or corrupt the runtime).
# Plugin config fields are deliberately not range-checked (their schemas own
# their semantics). The bound set below is the complete per_agent overlay
# surface — every field with a universe is listed; the rest are bool / list /
# Literal / free-form str, already fully enforced by their own types or the
# field validators in base/config/*.py.
_FRAMEWORK_RANGE_VALIDATORS: dict[str, Callable[[object], str | None]] = {
    # model-name universe
    "llm_model": _validate_model_membership,
    "memory_recall_filter_model": _validate_model_membership,
    # reasoning-effort vocabulary ("" pins the provider default; None = unset)
    "reasoning_effort": _validate_reasoning_effort_range,
    # durations / timeouts — strictly positive and finite
    "gemini_cache_timeout_seconds": _range_validator(gt=0),
    "heartbeat_pause_max_seconds": _range_validator(gt=0),
    "llm_stream_inter_chunk_timeout_seconds": _range_validator(gt=0),
    "llm_stream_total_timeout_seconds": _range_validator(gt=0),
    "llm_stream_ttft_timeout_seconds": _range_validator(gt=0),
    "memory_recall_filter_timeout_seconds": _range_validator(gt=0),
    "memory_recall_deadline_seconds": _range_validator(gt=0),
    # fractions — (0, 1]
    "auto_compact_fraction": _range_validator(gt=0, le=1),
    "compact_reminder_fraction": _range_validator(gt=0, le=1),
    # counts / budgets — non-negative and finite
    "auto_compact_ceiling_tokens": _range_validator(ge=0),
    "claude_thinking_budget_tokens": _range_validator(ge=0),
    "history_dump_keep": _range_validator(ge=0),
    "memory_recall_filter_max_retries": _range_validator(ge=0),
    "memory_recall_inject_k": _range_validator(ge=0),
    "memory_recall_retrieve_k": _range_validator(ge=0),
}


def _validate_framework_overlay_ranges(updates: dict[str, object]) -> None:
    for field, value in updates.items():
        validator = _FRAMEWORK_RANGE_VALIDATORS.get(field)
        if validator is None:
            continue
        error = validator(value)
        if error is not None:
            raise InvalidConfigOverlay(f"overlay key {field!r} {error}")


def validate_config_overlay(overlay: dict[str, object]) -> None:
    """SDK-side validation — called before `ava.self.restart(config_overlay=overlay)` writes to DB.

    Type validation relies on Pydantic: splice overlay into a fresh declaring
    sub-model instance and try to build (framework field via its declaring
    model, plugin field via the corresponding cls). The overlay surface spans
    every config domain (28 of 38 fields are agent-domain), while a boundary
    gateway or ops process constructs only its profile's domains. Reading the
    old process settings therefore raised the profile fail-fast AttributeError
    at the HTTP boundary (500, task #4423). A fresh sub-model starts from env +
    defaults, matching the child settings base before its overlay is applied, so
    the validation verdict cannot depend on the validating process. Framework
    fields with a named value universe then run their semantic range validator.
    Failure raises InvalidConfigOverlay.

    Success = overlay is valid; does **not** modify settings / _PLUGIN_CONFIGS —
    that's `apply_config_overlay`'s job at new process boot.
    """
    from base.config import field_domain
    from base.config.service_read import domain_model_classes

    targets = resolve_overlay_targets(overlay)
    grouped: dict[str | None, dict[str, object]] = {}
    for key, (plugin, field) in targets.items():
        grouped.setdefault(plugin, {})[field] = overlay[key]

    for plugin, updates in grouped.items():
        try:
            if plugin is None:
                # Validate each framework field against its owning sub-model — the
                # decomposed `settings` holds the field on `settings.<domain>`, so a
                # flat {field: value} overlay is grouped by domain and each sub-model
                # re-validated with the update merged in (runs its field/model
                # validators, e.g. the comma-list split).
                by_domain: dict[str, dict[str, object]] = {}
                for f, v in updates.items():
                    by_domain.setdefault(field_domain(f), {})[f] = v
                for dom, upd in by_domain.items():
                    cls = domain_model_classes()[dom]
                    sub = cls()
                    cls.model_validate({**sub.model_dump(), **upd})
            else:
                # The gateway and ops bind no plugin config: validate against the declared class,
                # layered over this process's bound instance when it has one, else the defaults.
                cls = overlay_config_classes()[plugin]
                held = _PLUGIN_CONFIGS.get(plugin)
                current = (held if held is not None else cls()).model_dump()
                cls(**{**current, **updates})
        except ValidationError as e:
            owner = "framework" if plugin is None else f"plugin {plugin!r}"
            raise InvalidConfigOverlay(
                f"overlay field type validation failed ({owner}): {e}"
            ) from e

    if None in grouped:
        _validate_framework_overlay_ranges(grouped[None])


def apply_config_overlay(
    overlay: dict[str, object],
    *,
    scope: Literal["framework", "plugin", "all"] = "all",
) -> None:
    """Boot-time apply — merge overlay into framework settings + plugin configs.

    Framework keys (e.g. `llm_model`) need to be applied **before** the process
    reads them — an embedding driver can build the LLM client off
    `settings.lm.llm_model` early. Plugin keys must be applied **after**
    `bind_plugin_config()` populates `_PLUGIN_CONFIGS`. The two phases are
    different stages of the boot sequence, so the caller splits the overlay
    into two calls with `scope="framework"` (early) and `scope="plugin"`
    (late). `scope="all"` keeps the single-call behavior for callers that
    don't care about phase (tests, off-process diagnostics).

    Framework overlay: `base_config.set_field(field, value)` for each key —
    in-place mutation on the owning sub-model of the singleton instance. Every
    module that has captured `from base.config import settings` sees the change
    (same sub-model object). Matches the pattern used by tests/fixtures/provisioning.py to point
    Settings at the per-session DB url.
    Plugin Config overlay: `_PLUGIN_CONFIGS[plugin] = cls(**merged)`, new
    frozen instance.

    Field-level type and named-range validation happens up-front via
    `validate_config_overlay` before the inbound is committed, so by the time
    this runs the values are already known good for their declared contracts.

    Raises:
        InvalidConfigOverlay: overlay invalid (key / type / per_agent).
    """
    import base.config as base_config

    targets = resolve_overlay_targets(overlay)
    grouped: dict[str | None, dict[str, object]] = {}
    for key, (plugin, field) in targets.items():
        if scope == "framework" and plugin is not None:
            continue
        if scope == "plugin" and plugin is None:
            continue
        grouped.setdefault(plugin, {})[field] = overlay[key]

    for plugin, updates in grouped.items():
        try:
            if plugin is None:
                for field, value in updates.items():
                    base_config.set_field(field, value)
            else:
                cls = _PLUGIN_CONFIG_CLASSES[plugin]
                current = _PLUGIN_CONFIGS[plugin].model_dump()
                _PLUGIN_CONFIGS[plugin] = cls(**{**current, **updates})
        except ValidationError as e:
            owner = "framework" if plugin is None else f"plugin {plugin!r}"
            raise InvalidConfigOverlay(
                f"overlay field type validation failed ({owner}): {e}"
            ) from e


def _field_is_sensitive(extra: object) -> bool:
    """True when a field's `json_schema_extra` marks it `sensitive=True`.

    SecretStr values are already masked by pydantic's model_dump, but a
    sensitive field that is NOT a SecretStr (or one that later changes type)
    would otherwise land in the snapshot verbatim — filter the marker, not the
    value shape.
    """
    if not isinstance(extra, dict):
        return False
    extras = cast(dict[str, Any], extra)
    return extras.get("sensitive") is True


def _framework_field_is_sensitive(name: str) -> bool:
    """Framework-field sensitive lookup — FIELD_INFOS maps field name -> FieldInfo."""
    from base.config import FIELD_INFOS

    info = FIELD_INFOS.get(name)
    if info is None:
        return False
    return _field_is_sensitive(info.json_schema_extra)


def effective_config_snapshot() -> dict[str, object]:
    """Full snapshot of current framework + plugin config — used as `restart_completed`
    inbound payload, so the event trail records the config the new process actually runs with.

    Returns a flat dict (no nested plugin namespace), making it easy to diff against
    the previous snapshot. Framework fields are at the top level; plugin fields are
    prefixed with `<plugin>.<field>` to avoid colliding with framework field names
    (the same-name rejection rule in resolve_overlay_targets prevents collision on
    the overlay write path, but a snapshot is a superset and must distinguish strictly).

    Fields marked `sensitive=True` are excluded: the snapshot is stored in plain
    JSON on every restart_completed inbound row (2026-08-08 audit, P2-7), so a
    sensitive value must not get a second plaintext copy there even though
    SecretStr masking already redacts the value itself.
    """
    from base.config import flat_dump

    out: dict[str, object] = {
        name: value
        for name, value in flat_dump(mode="json").items()
        if not _framework_field_is_sensitive(name)
    }
    for plugin, instance in _PLUGIN_CONFIGS.items():
        fields = type(instance).model_fields
        for field, value in instance.model_dump(mode="json").items():
            info = fields[field].json_schema_extra
            if _field_is_sensitive(info):
                continue
            out[f"{plugin}.{field}"] = value
    return out


def registered_plugin_config_names() -> tuple[str, ...]:
    """Sorted names of plugins with a bound config instance."""
    return tuple(sorted(_PLUGIN_CONFIGS))


def is_per_agent_field(plugin: str, field: str) -> bool:
    """Whether the field is marked `json_schema_extra={"per_agent": True}` — used for
    PR-E CLI overlay validation (per_agent=False fields do not allow per-agent
    override, enforcing cluster consistency).

    plugin / field not found → False (this is a query helper, should not raise;
    let the CLI decide).
    """
    cls = _PLUGIN_CONFIG_CLASSES.get(plugin)
    if cls is None:
        return False
    info = cls.model_fields.get(field)
    if info is None:
        return False
    return bool(_schema_extra(info).get("per_agent", False))
