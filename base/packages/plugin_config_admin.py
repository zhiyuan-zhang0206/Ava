"""Config-service routing for schema-owned plugin images, with one owner per patch.

Declarations are discovered from existing plugin faces on each request. This is
not a second registry: names, field metadata and validation come from the pure
schema, while the image remains the sole persisted authority.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ValidationError

from base.config.admin.metadata import ConfigFieldMeta, env_override_values, get_config_metadata
from base.config.service_read import ConfigAuthority
from base.packages.plugin_config_images import (
    PluginConfigChangedError,
    PluginConfigOwner,
    image_revision,
    write_config_image,
)
from base.packages.plugins.config_face import declared_config_class
from base.packages.plugins.config_registration import (
    config_from_image,
    disk_image_path,
    merge_disk_image_schema,
    read_config_image,
)
from base.packages.plugins.enable_config import discover_plugins
from base.packages.plugins.plugin_errors import InvalidConfigOverlay


def config_owners() -> dict[str, PluginConfigOwner]:
    """Discover declared plugin schemas independently of agent enable state."""
    owners: dict[str, PluginConfigOwner] = {}
    for name, directory in discover_plugins().items():
        cls = declared_config_class(name, directory)
        if cls is not None:
            owners[name] = PluginConfigOwner(name, cls, disk_image_path(name))
    return owners


def plugin_field_owners() -> dict[str, PluginConfigOwner]:
    """Config-panel fields, uniquely attributed by their declared scope metadata."""
    from base.config import FIELD_INFOS, schema_extra

    fields: dict[str, PluginConfigOwner] = {}
    for owner in config_owners().values():
        for name, info in owner.cls.model_fields.items():
            if "scope" not in schema_extra(info):
                continue
            if name in FIELD_INFOS or name in fields:
                raise ValueError(f"config field {name!r} has more than one declaration owner")
            fields[name] = owner
    return fields


def patch_owner(fields: set[str]) -> PluginConfigOwner | None:
    """Return the one image owner, or None for Core; reject mixed-owner writes."""
    from base.config import FIELD_INFOS

    plugins = plugin_field_owners()
    owners: dict[str | None, PluginConfigOwner | None] = {}
    for name in fields:
        if name in plugins:
            owner = plugins[name]
            owners[owner.name] = owner
        elif name in FIELD_INFOS:
            owners[None] = None
        else:
            raise InvalidConfigOverlay(f"unknown field {name!r}")
    if len(owners) > 1:
        raise InvalidConfigOverlay("config patch mixes owners; send one request per config owner")
    return next(iter(owners.values()), None)


def plugin_metadata() -> list[ConfigFieldMeta]:
    """Derive panel metadata from image values and the owning schema."""
    from base.config import schema_extra
    from base.config.config_registry import field_editor_type

    result: list[ConfigFieldMeta] = []
    fields = plugin_field_owners()
    for owner in config_owners().values():
        names = [name for name, assigned in fields.items() if assigned.name == owner.name]
        if not names:
            continue
        config = read_config_image(owner.cls, owner.path)
        for name in names:
            info = owner.cls.model_fields[name]
            extra = schema_extra(info)
            kind, choices = field_editor_type(info.annotation)
            result.append(
                ConfigFieldMeta(
                    name=name,
                    field_type=kind,
                    current_value=getattr(config, name),
                    default_value=info.default,
                    description=info.description or "",
                    group=f"Plugin: {owner.name}",
                    restart_required=extra.get("restart_required", "all"),
                    writable=extra.get("writable", True),
                    sensitive=extra.get("sensitive", False),
                    env_var=extra.get("env_var", ""),
                    scope=extra["scope"],
                    capability=extra.get("capability", "common"),
                    remote_writable=extra.get("remote_writable", False),
                    per_agent=extra.get("per_agent", False),
                    choices=choices,
                    owner=owner.name,
                )
            )
    return result


def plugin_overrides() -> dict[str, object]:
    """Present image values for panel reducers; defaults of absent images are not overrides."""
    fields = plugin_field_owners()
    values: dict[str, object] = {}
    for owner in config_owners().values():
        if not owner.path.exists():
            continue
        data = read_config_image(owner.cls, owner.path).model_dump(mode="json")
        values.update(
            {
                name: value
                for name, value in data.items()
                if name in fields and fields[name].name == owner.name
            }
        )
    return values


def write_plugin_patch(
    owner: PluginConfigOwner,
    updates: dict[str, object],
    removals: set[str],
    *,
    expected_digest: str | None,
) -> None:
    """Validate one owner's whole candidate and preserve other image fields."""
    try:
        captured = owner.path.read_bytes()
    except FileNotFoundError:
        captured = None
    captured_digest = image_revision(captured)
    if expected_digest is not None and expected_digest != captured_digest:
        raise PluginConfigChangedError("plugin config changed before owned image write")
    config = (
        config_from_image(owner.cls, captured.decode(), owner.path)
        if captured is not None
        else owner.cls()
    )
    current = config.model_dump()
    for name in removals:
        current[name] = owner.cls.model_fields[name].get_default(call_default_factory=True)
    try:
        candidate = owner.cls.model_validate({**current, **updates})
    except ValidationError as exc:
        raise InvalidConfigOverlay(str(exc)) from exc
    write_config_image(owner, candidate, expected_digest=captured_digest)


def panel_metadata(*, authority: ConfigAuthority) -> list[ConfigFieldMeta]:
    """Core and plugin-owned config-panel metadata, in panel order."""
    return [*get_config_metadata(authority=authority), *plugin_metadata()]


def panel_overrides(*, authority: ConfigAuthority, local: bool = False) -> dict[str, Any]:
    """The editable override set of core `.env` fields and plugin-owned images."""
    return env_override_values(
        authority=authority,
        local=local,
        plugin_metadata=plugin_metadata(),
        plugin_values=plugin_overrides(),
    )


class PluginUpdateEntry(BaseModel):
    """Result for a single plugin after disk-image schema merge."""

    name: str
    status: Literal["updated", "no_diff", "skipped", "error"]
    added: list[str] = []
    removed: list[str] = []
    detail: str | None = None  # reason for skipped; exception message for error


class PluginUpdateResult(BaseModel):
    """Response body of `update_all_disk_images()`."""

    entries: list[PluginUpdateEntry]


def update_all_disk_images() -> PluginUpdateResult:
    """Scan all plugins, auto-merge disk-image schema diff, return structured result.

    Driven by the `meiva plugins update` CLI (`cli/commands/extensions/plugins.py`); the
    `meiva start` converge step runs the same path. There is no gateway endpoint
    for it. Each plugin goes through
    `base.packages.plugins.config_registration.merge_disk_image_schema(name, Cls)`:
      - disk image missing -> write default
      - new field -> fill into disk image with cls default
      - removed field -> dropped from the disk image (so the image converges to
        the current schema and agent spawns stop hitting SchemaDriftError)
      - type incompatible -> that entry status='error'; does not
        interrupt other plugins

    Takes each plugin's class from the `contribute()` of its `default_config.py` config face
    (`base.packages.plugins.config_face`); `plugin.py` is never imported.
    """
    entries = [
        _update_one_disk_image(name, plugin_dir)
        for name, plugin_dir in sorted(discover_plugins().items())
    ]
    return PluginUpdateResult(entries=entries)


def _update_one_disk_image(name: str, plugin_dir: Path) -> PluginUpdateEntry:
    """Merge the config class one plugin declares into its disk image; never raises."""
    try:
        cls = declared_config_class(name, plugin_dir)
        if cls is None:
            return PluginUpdateEntry(name=name, status="skipped", detail="no default_config.py")
        added, removed = merge_disk_image_schema(name, cls)
    except Exception as e:
        return PluginUpdateEntry(name=name, status="error", detail=str(e))

    if not added and not removed:
        return PluginUpdateEntry(name=name, status="no_diff")
    return PluginUpdateEntry(
        name=name, status="updated", added=sorted(added), removed=sorted(removed)
    )
