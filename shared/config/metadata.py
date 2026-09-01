"""Frontend config-panel metadata export (a consumer of the field registry).

`ConfigFieldMeta` / `get_config_metadata` / `env_override_values` serialize the
flat field registry + current values for GET /api/config; the write path's
"keep existing value" sentinel lives here too. Split out of
`shared/config/__init__.py` for the design's line budget (R2 convergence point
A re-split): the registry is built in `shared/config_registry.py`, Settings and
its boot consumers stay in `__init__.py`.

Imports of the config package are function-level (this module is imported by
`shared/config/__init__.py` at its tail for the re-exports; a top-level import
would be circular).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from pydantic_core import PydanticUndefined

from shared.config_registry import Capability, field_editor_type

# Stand-in a sensitive field carries in `raw_overrides` instead of its cleartext
# value: the panel/CLI round-trips it unchanged on a full-replace PUT, and the
# write path treats it as "keep the existing .env value" (neither rewrite nor
# unset). This keeps secrets off the wire without a full-replace clobbering them.
CONFIG_UNCHANGED_SENTINEL = "__ava_unchanged__"


class ConfigFieldMeta:
    """Full metadata for a single config field — serialized by GET /api/config for the frontend."""

    def __init__(
        self,
        name: str,
        field_type: str,
        current_value: Any,
        default_value: Any,
        description: str,
        group: str,
        restart_required: str,
        *,
        writable: bool,
        sensitive: bool,
        env_var: str,
        scope: str,
        capability: Capability,
        remote_writable: bool,
        per_agent: bool,
        choices: list[str] | None = None,
    ) -> None:
        self.name = name
        self.field_type = field_type
        self.current_value = current_value
        self.default_value = default_value
        self.description = description
        self.group = group
        self.restart_required = restart_required
        self.writable = writable
        self.sensitive = sensitive
        self.env_var = env_var
        self.scope = scope
        # gateway | agent-runner | common — conceptual ownership, not panel display grouping.
        self.capability: Capability = capability
        self.remote_writable = remote_writable
        self.per_agent = per_agent
        # For an "enum" field, the allowed values (the Literal members) the
        # frontend renders as a fixed-option select; None for every other type.
        self.choices = choices


def get_config_metadata() -> list[ConfigFieldMeta]:
    """Walk every sub-model's fields and extract the metadata the frontend needs."""
    from shared.config import _FIELDS, _schema_extra, current_field_values, field_alias

    values = current_field_values()
    result: list[ConfigFieldMeta] = []
    for name, ref in _FIELDS.items():
        field_info = ref.info
        extra = _schema_extra(field_info)
        type_name, choices = field_editor_type(field_info.annotation)

        current = values[name]
        if isinstance(current, Path):
            current = str(current)

        default = field_info.default
        if default is PydanticUndefined:
            # Required fields (db_url / redis_url etc. with no default) leaking the
            # sentinel would cause FastAPI's ConfigFieldView serialization of the
            # whole response to blow up with 500.
            default = None
        elif isinstance(default, Path):
            default = str(default)

        # remote_writable is required on host-scope fields (hard index raises
        # KeyError if a host field forgot to declare it); non-host fields default False.
        scope = extra["scope"]
        remote_writable = extra["remote_writable"] if scope == "host" else False

        result.append(
            ConfigFieldMeta(
                name=name,
                field_type=type_name,
                current_value=current,
                default_value=default,
                description=field_info.description or "",
                group=ref.group,
                restart_required=extra.get("restart_required", "all"),
                writable=extra.get("writable", True),
                sensitive=extra.get("sensitive", False),
                env_var=field_alias(name),
                scope=scope,
                capability=ref.capability,
                remote_writable=remote_writable,
                per_agent=extra.get("per_agent") is True,
                choices=choices,
            )
        )
    return result


def env_override_values(*, local: bool = False) -> dict[str, Any]:
    """field name -> current value for every field explicitly set in this unit's
    `.env` that a PUT /api/config would accept.

    This is the editable override set the config panel deltas against. A field is
    included only if it is `writable`; a host-scope field additionally requires
    `remote_writable` UNLESS `local` (a self-target edit), since `writable` already
    means "a human may edit it on its own host" and `remote_writable` only gates
    editing a *remote* host's field. That keeps read-only fields (`db_url`) out and,
    for a remote target, the connection / identity values install/enroll wrote — so
    the whole set round-trips back through a PUT without rejection.
    """
    from shared import runtime_config
    from shared.config import current_field_values

    set_fields = runtime_config.env_set_field_names()
    values = current_field_values()
    out: dict[str, Any] = {}
    for meta in get_config_metadata():
        if meta.name not in set_fields or not meta.writable:
            continue
        if meta.scope == "host" and not local and not meta.remote_writable:
            continue
        if meta.sensitive:
            # Never put a secret's cleartext on the wire. The panel/CLI carries the
            # sentinel and PUTs it back unchanged; the write path preserves the
            # existing .env value (set the field with a real value to change it,
            # `ava config unset` to clear it).
            out[meta.name] = CONFIG_UNCHANGED_SENTINEL
            continue
        value = values[meta.name]
        if isinstance(value, Path):
            value = str(value)
        out[meta.name] = value
    return out
