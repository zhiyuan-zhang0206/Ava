"""Config-service routing for schema-owned plugin images, with one owner per patch.

Declarations are discovered from existing plugin faces on each request. This is
not a second registry: names, field metadata and validation come from the pure
schema, while the image remains the sole persisted authority.
"""

from __future__ import annotations

from base.config.admin.metadata import ConfigFieldMeta
from base.packages.plugin_config_images import (
    PluginConfigOwner,
    image_revision,
    write_config_image,
)
from base.packages.plugins.config_face import declared_config_class
from base.packages.plugins.config_registration import (
    config_from_image,
    disk_image_path,
    read_authority_config,
)


def config_owners() -> dict[str, PluginConfigOwner]:
    """Discover declared plugin schemas independently of agent enable state."""
    from base.packages.plugins.enable_config import discover_plugins

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
            raise ValueError(f"unknown field {name!r}")
    if len(owners) > 1:
        raise ValueError("config patch mixes owners; send one request per config owner")
    return next(iter(owners.values()), None)


def plugin_metadata() -> list[ConfigFieldMeta]:
    """Derive panel metadata from image values and the owning schema."""
    from base.config import schema_extra
    from base.host.env.config_registry import field_editor_type

    result: list[ConfigFieldMeta] = []
    fields = plugin_field_owners()
    for owner in config_owners().values():
        names = [name for name, assigned in fields.items() if assigned.name == owner.name]
        if not names:
            continue
        config = read_authority_config(owner.name, owner.cls, owner.path)
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
        data = read_authority_config(owner.name, owner.cls, owner.path).model_dump(mode="json")
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
        raise RuntimeError("plugin config changed before owned image write")
    config = (
        config_from_image(owner.cls, captured.decode(), owner.path)
        if captured is not None
        else owner.cls()
    )
    current = config.model_dump()
    for name in removals:
        current[name] = owner.cls.model_fields[name].get_default(call_default_factory=True)
    candidate = owner.cls.model_validate({**current, **updates})
    write_config_image(owner, candidate, expected_digest=captured_digest)


def import_legacy_config(owner: PluginConfigOwner) -> bool:
    """Import declared old env inputs once, preserving conflicts and retryability.

    Commit the image first. Removing the old aliases is a separate owned write;
    failure leaves the imported image and legacy values available for a same-value
    retry. No multi-file transaction is promised.
    """
    from base.config import schema_extra
    from base.host.env import runtime_config
    from base.host.env.bootstrap import consume_legacy_plugin_env, legacy_plugin_config_values
    from base.host.env.dotenv_file import remove_env

    aliases = {
        schema_extra(info)["env_var"]: name
        for name, info in owner.cls.model_fields.items()
        if "env_var" in schema_extra(info)
    }
    legacy = legacy_plugin_config_values(tuple(aliases))
    if not legacy:
        return False
    captured = owner.path.read_text() if owner.path.exists() else None
    current = (
        config_from_image(owner.cls, captured, owner.path) if captured is not None else owner.cls()
    )
    updates = {aliases[alias]: value for alias, value in legacy.items()}
    imported = owner.cls.model_validate({**current.model_dump(), **updates})
    if captured is not None:
        conflicts = [name for name in updates if getattr(current, name) != getattr(imported, name)]
        if conflicts:
            raise ValueError(
                f"plugin {owner.name!r} image conflicts with legacy env fields {sorted(conflicts)}"
            )
    write_config_image(
        owner,
        imported,
        expected_digest=image_revision(captured.encode() if captured is not None else None),
    )
    remove_env(runtime_config.env_file_path(), set(legacy), expected_values=legacy)
    consume_legacy_plugin_env(legacy)
    return True
