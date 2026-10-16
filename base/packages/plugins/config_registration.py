import json
import math
from collections.abc import Callable, Mapping
from contextlib import suppress
from pathlib import Path
from typing import Any, Literal, cast, overload

from pydantic import BaseModel, ValidationError
from pydantic.fields import FieldInfo

from base.host.env.agent_slices import AgentSlices
from base.packages.plugins.plugin_errors import (
    DuplicateRegistration,
    InvalidConfigData,
    InvalidConfigOverlay,
    SchemaDriftError,
)
from base.paths.home import resolve_ava_home


def bind_plugin_config(
    plugin: str, cls: type[BaseModel], configs: dict[str, BaseModel]
) -> Callable[[], None]:
    if not (isinstance(cls, type) and issubclass(cls, BaseModel)):
        raise TypeError(
            f"plugin {plugin!r} declared config {cls!r}, which is not a BaseModel subclass — "
            f"write `class FooConfig(BaseModel): ...` and declare it in `PluginContributions.config`."
        )
    if plugin in configs:
        raise DuplicateRegistration(
            f"plugin {plugin!r} already has a bound config ({type(configs[plugin]).__name__})"
        )
    instance = _instantiate_from_disk(plugin, cls)
    configs[plugin] = instance

    def undo() -> None:
        configs.pop(plugin, None)

    return undo


def _instantiate_from_disk(plugin: str, cls: type[BaseModel]) -> BaseModel:
    from base.packages.plugin_config_images import PluginConfigChangedError
    from base.packages.plugins.config_view import with_cluster_policy

    config_path = disk_image_path(plugin)
    read_plugin_config(plugin, cls, config_path)
    if not config_path.exists():
        with suppress(PluginConfigChangedError):
            write_default_disk_image(plugin, cls)
    return with_cluster_policy(plugin, config_from_image(cls, config_path.read_text(), config_path))


def read_config_image[C: BaseModel](cls: type[C], config_path: Path) -> C:
    try:
        content = config_path.read_text()
    except FileNotFoundError:
        return cls()

    return config_from_image(cls, content, config_path)


def config_from_image[C: BaseModel](cls: type[C], content: str, config_path: Path) -> C:
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
    return resolve_ava_home().resolve() / "configs" / plugin / "config.json"


def write_default_disk_image(plugin: str, cls: type[BaseModel]) -> Path:
    instance = cls()
    config_path = disk_image_path(plugin)
    config_path.parent.mkdir(parents=True, exist_ok=True)
    from base.packages.plugin_config_images import (
        PluginConfigOwner,
        image_revision,
        write_config_image,
    )

    write_config_image(
        PluginConfigOwner(plugin, cls, config_path),
        instance,
        expected_digest=image_revision(None),
    )
    return config_path


def reconcile_config_image[C: BaseModel](
    cls: type[C], content: str | None, config_path: Path
) -> tuple[C, set[str], set[str]]:
    if content is None:
        return cls(), set(cls.model_fields), set()
    try:
        data = json.loads(content)
    except json.JSONDecodeError as exc:
        raise InvalidConfigData(f"plugin disk image JSON malformed ({config_path}): {exc}") from exc
    if not isinstance(data, dict):
        raise InvalidConfigData(f"plugin config disk image must be a JSON object ({config_path})")
    data = cast("dict[str, object]", data)
    disk_keys = set(data)
    cls_keys = set(cls.model_fields)
    defaults = cls().model_dump(mode="json")
    merged = {field: data[field] if field in disk_keys else defaults[field] for field in cls_keys}
    try:
        candidate = cls.model_validate(merged)
    except ValidationError as exc:
        raise InvalidConfigData(
            f"plugin disk image post-merge Pydantic validation failed ({config_path}): {exc}"
        ) from exc
    return candidate, cls_keys - disk_keys, disk_keys - cls_keys


# ava:ref base/packages/plugins/docs/plugin-config/plugin-config.ava.okf.md#schema-reconciliation
def merge_disk_image_schema(plugin: str, cls: type[BaseModel]) -> tuple[set[str], set[str]]:
    config_path = disk_image_path(plugin)

    if not config_path.exists():
        write_default_disk_image(plugin, cls)
        return set(cls.model_fields.keys()), set()

    captured = config_path.read_text()
    candidate, added, removed = reconcile_config_image(cls, captured, config_path)
    if not added and not removed:
        return set(), set()

    from base.packages.plugin_config_images import (
        PluginConfigOwner,
        image_revision,
        write_config_image,
    )

    write_config_image(
        PluginConfigOwner(plugin, cls, config_path),
        candidate,
        expected_digest=image_revision(captured.encode()),
    )
    return added, removed


@overload
def get_plugin_config(plugin: str, slices: AgentSlices) -> BaseModel: ...
@overload
def get_plugin_config[T: BaseModel](plugin: str, slices: AgentSlices, cls: type[T]) -> T: ...


def get_plugin_config(
    plugin: str, slices: AgentSlices, cls: type[BaseModel] | None = None
) -> BaseModel:
    _ = cls
    return slices.plugin_config(plugin)


def all_plugin_configs(slices: AgentSlices) -> dict[str, BaseModel]:
    return slices.plugin_configs()


def process_plugin_config(plugin: str, configs: Mapping[str, BaseModel]) -> BaseModel:
    return configs[plugin]


def _schema_extra(info: FieldInfo) -> dict[str, Any]:
    extra = info.json_schema_extra
    return cast("dict[str, Any]", extra) if isinstance(extra, dict) else {}


def _per_agent_fields_for(cls: type[BaseModel]) -> set[str]:
    out: set[str] = set()
    for name, info in cls.model_fields.items():
        if _schema_extra(info).get("per_agent") is True:
            out.add(name)
    return out


def overlay_config_classes(
    configs: Mapping[str, BaseModel] | None = None,
) -> dict[str, type[BaseModel]]:
    from base.packages.plugins import enable_config
    from base.packages.plugins.config_face import enabled_config_classes

    installed = enable_config.installed_plugin_dirs()
    config = enable_config.load_for_runtime(set(installed))
    enabled = {
        name: plugin_dir
        for name, plugin_dir in installed.items()
        if name in config.plugins and config.plugins[name].enabled
    }
    return {
        **enabled_config_classes(enabled),
        **{plugin: type(instance) for plugin, instance in (configs or {}).items()},
    }


# ava:ref base/packages/plugins/docs/plugin-config/plugin-config.ava.okf.md#overlay-admission
def resolve_overlay_targets(
    overlay: dict[str, object], configs: Mapping[str, BaseModel] | None = None
) -> dict[str, tuple[str | None, str]]:
    from base.config import field_names, per_agent_field_names

    framework_per_agent = per_agent_field_names()
    framework_all = field_names()

    plugin_classes = overlay_config_classes(configs)
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


def _validate_model_membership(
    value: object, models: Mapping[str, object], *, kind: str = "model"
) -> str | None:
    if isinstance(value, str) and value in models:
        return None
    valid_models = ", ".join(sorted(models))
    return f"value {value!r} is not a registered {kind}; valid {kind}s: {valid_models}"


def _validate_reasoning_effort_range(value: object) -> str | None:
    from base.lm.effort import EFFORT_VOCAB

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


# ava:ref base/packages/plugins/docs/plugin-config/plugin-config.ava.okf.md#framework-range-validators
_FRAMEWORK_RANGE_VALIDATORS: dict[str, Callable[[object], str | None]] = {
    "reasoning_effort": _validate_reasoning_effort_range,
    "heartbeat_pause_max_seconds": _range_validator(gt=0),
    "llm_stream_inter_chunk_timeout_seconds": _range_validator(gt=0),
    "llm_stream_total_timeout_seconds": _range_validator(gt=0),
    "llm_stream_ttft_timeout_seconds": _range_validator(gt=0),
    "memory_recall_deadline_seconds": _range_validator(gt=0),
    "skill_recall_deadline_seconds": _range_validator(gt=0),
    "memory_write_check_deadline_seconds": _range_validator(gt=0),
    "memory_recall_inject_threshold": _range_validator(ge=0, le=1),
    "memory_write_dedup_threshold": _range_validator(ge=0, le=1),
    "auto_compact_fraction": _range_validator(gt=0, le=1),
    "compact_reminder_fraction": _range_validator(gt=0, le=1),
    "skill_recall_threshold": _range_validator(gt=0, le=1),
    "auto_compact_ceiling_tokens": _range_validator(ge=0),
    "claude_thinking_budget_tokens": _range_validator(ge=0),
    "history_dump_keep": _range_validator(ge=0),
    "memory_recall_inject_max": _range_validator(ge=0),
    "skill_recall_max": _range_validator(ge=0),
    "to_user_soft_limit_words": _range_validator(gt=0),
}


def _validate_framework_overlay_ranges(updates: dict[str, object]) -> None:
    for field, value in updates.items():
        validator = _FRAMEWORK_RANGE_VALIDATORS.get(field)
        if validator is None:
            continue
        error = validator(value)
        if error is not None:
            raise InvalidConfigOverlay(f"overlay key {field!r} {error}")


# ava:ref base/packages/plugins/docs/plugin-config/plugin-config.ava.okf.md#overlay-admission
def validate_config_overlay_shape(
    overlay: dict[str, object], configs: Mapping[str, BaseModel] | None = None
) -> None:
    from base.config import field_domain
    from base.config.service_read import domain_model_classes

    targets = resolve_overlay_targets(overlay, configs)
    grouped: dict[str | None, dict[str, object]] = {}
    for key, (plugin, field) in targets.items():
        grouped.setdefault(plugin, {})[field] = overlay[key]

    for plugin, updates in grouped.items():
        try:
            if plugin is None:
                by_domain: dict[str, dict[str, object]] = {}
                for f, v in updates.items():
                    by_domain.setdefault(field_domain(f), {})[f] = v
                for dom, upd in by_domain.items():
                    cls = domain_model_classes()[dom]
                    sub = cls()
                    cls.model_validate({**sub.model_dump(), **upd})
            else:
                cls = overlay_config_classes(configs)[plugin]
                held = None if configs is None else configs.get(plugin)
                current = (held if held is not None else cls()).model_dump()
                cls(**{**current, **updates})
        except ValidationError as e:
            owner = "framework" if plugin is None else f"plugin {plugin!r}"
            raise InvalidConfigOverlay(
                f"overlay field type validation failed ({owner}): {e}"
            ) from e

    if None in grouped:
        _validate_framework_overlay_ranges(grouped[None])


_DECISION_MODEL_FIELDS = ("skill_recall_model",)


def validate_config_overlay(
    overlay: dict[str, object],
    configs: Mapping[str, BaseModel] | None = None,
    *,
    models: Mapping[str, object],
    decision_models: Mapping[str, object] | None = None,
) -> None:
    validate_config_overlay_shape(overlay, configs)
    if "llm_model" in overlay:
        error = _validate_model_membership(overlay["llm_model"], models)
        if error is not None:
            raise InvalidConfigOverlay(f"overlay key 'llm_model' {error}")
    for field in _DECISION_MODEL_FIELDS:
        if field in overlay:
            if decision_models is None:
                raise InvalidConfigOverlay(
                    f"overlay key {field!r} names a decision model, but the caller supplied "
                    "no decision-model table to check it against"
                )
            error = _validate_model_membership(
                overlay[field], decision_models, kind="decision model"
            )
            if error is not None:
                raise InvalidConfigOverlay(f"overlay key {field!r} {error}")


def apply_config_overlay(
    overlay: dict[str, object],
    *,
    configs: Mapping[str, BaseModel] | None = None,
    scope: Literal["framework", "plugin", "all"] = "all",
    set_framework_field: Callable[[str, object], None] | None = None,
) -> dict[str, BaseModel]:
    targets = resolve_overlay_targets(overlay, configs)
    resolved = dict(configs or {})
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
                if set_framework_field is None:
                    raise RuntimeError(
                        "framework overlay requires its configuration owner's setter"
                    )
                for field, value in updates.items():
                    set_framework_field(field, value)
            else:
                instance = resolved[plugin]
                resolved[plugin] = type(instance)(**{**instance.model_dump(), **updates})
        except ValidationError as e:
            owner = "framework" if plugin is None else f"plugin {plugin!r}"
            raise InvalidConfigOverlay(
                f"overlay field type validation failed ({owner}): {e}"
            ) from e
    return resolved


def _field_is_sensitive(extra: object) -> bool:
    if not isinstance(extra, dict):
        return False
    extras = cast(dict[str, Any], extra)
    return extras.get("sensitive") is True


def registered_plugin_config_names(configs: Mapping[str, BaseModel]) -> tuple[str, ...]:
    return tuple(sorted(configs))


def is_per_agent_field(plugin: str, field: str, configs: Mapping[str, BaseModel]) -> bool:
    instance = configs.get(plugin)
    if instance is None:
        return False
    info = type(instance).model_fields.get(field)
    if info is None:
        return False
    return bool(_schema_extra(info).get("per_agent", False))


def service_config_packet(plugin: str, config: BaseModel) -> str:
    for name, field in type(config).model_fields.items():
        if _field_is_sensitive(field.json_schema_extra):
            raise InvalidConfigData(f"service config {plugin}.{name} is sensitive")
    return json.dumps(
        {"plugin": plugin, "config": config.model_dump(mode="json")},
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


# ava:ref base/packages/plugins/docs/plugin-config/plugin-config.ava.okf.md#service-birth-snapshot
def read_service_config[C: BaseModel](plugin: str, cls: type[C]) -> C | None:
    from base.host.env.bootstrap import service_plugin_config_packet

    packet = service_plugin_config_packet()
    if packet is None:
        return None
    try:
        data: object = json.loads(packet)
    except json.JSONDecodeError as exc:
        raise InvalidConfigData(f"invalid service plugin config JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise InvalidConfigData("service plugin config requires a JSON object")
    data = cast("dict[str, object]", data)
    if set(data) != {"plugin", "config"}:
        raise InvalidConfigData("service plugin config requires exactly plugin and config")
    if data["plugin"] != plugin:
        raise InvalidConfigData(
            f"service plugin config names {data['plugin']!r}, expected {plugin!r}"
        )
    values = data["config"]
    if not isinstance(values, dict) or set(cast("dict[str, object]", values)) != set(
        cls.model_fields
    ):
        raise SchemaDriftError(
            f"service plugin config {plugin!r} field set differs from declaration"
        )
    try:
        return cls.model_validate(values)
    except ValidationError as exc:
        raise InvalidConfigData(f"invalid service plugin config {plugin!r}: {exc}") from exc


def read_plugin_config[C: BaseModel](plugin: str, cls: type[C], config_path: Path) -> C:
    from base.packages.plugins.config_view import with_cluster_policy

    return with_cluster_policy(plugin, read_config_image(cls, config_path))


def plugin_bootstrap_config() -> str:
    import json

    from base.config import schema_extra
    from base.packages.plugins.config_face import declared_config_class
    from base.packages.plugins.enable_config import discover_plugins

    payload: dict[str, dict[str, object]] = {}
    for plugin, plugin_dir in sorted(discover_plugins().items()):
        cls = declared_config_class(plugin, plugin_dir)
        if cls is None:
            continue
        fields: list[str] = []
        for name, info in cls.model_fields.items():
            extra = schema_extra(info)
            if extra.get("scope") not in {
                "cluster-pinned",
                "cluster-default",
            }:
                continue
            if extra.get("sensitive"):
                raise ValueError(f"plugin cluster config {plugin}.{name} may not carry secrets")
            fields.append(name)
        if fields:
            config = read_config_image(cls, disk_image_path(plugin))
            values = config.model_dump(mode="json")
            payload[plugin] = {name: values[name] for name in fields}
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False)
