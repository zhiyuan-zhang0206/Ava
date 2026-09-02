"""Plugin access to declared, non-sensitive core configuration flags.

Plugins declare every core flag they read at the top level of ``plugin.py``:

    from shared.plugin_flags import declare_flags

    declare_flags(
        "agent.prompt_invest_future_enabled",
        "agent.agent_communication_style",
    )

Later, plugin behavior reads a declared flag from the current turn:

    from shared.plugin_flags import read_flag

    if read_flag("agent.prompt_invest_future_enabled"):
        ...

Declaration is mandatory: ``read_flag`` rejects a key the current plugin did
not declare. Keys are fully qualified as ``<domain>.<field>``. The namespace is
every non-sensitive core Settings field; secrets remain in their existing
secret channels and are never flags.

Reads use the per-turn settings view, so per-agent pins and the agent's model
apply. Model-tuning fields resolve with the same explicit-value, model-default,
then shared-default layering used by the framework; other fields return their
turn-view value directly. A cluster config change takes effect on the next
process or agent start: values are read at start, not live.
"""

from typing import Any

from shared.config_registry import _DOMAIN_ATTRS, _fields
from shared.plugin_config_registry import _field_is_sensitive
from shared.plugin_context import current_plugin_name


class PluginFlagError(Exception):
    """Root of plugin core-flag declaration and read failures."""


class NoPluginContext(PluginFlagError):  # noqa: N818
    """A plugin flag API was called outside the plugin import or behavior context."""


class UnknownFlag(PluginFlagError):  # noqa: N818
    """A declared key is malformed, unknown, or names a sensitive core field."""


class UndeclaredFlag(PluginFlagError):  # noqa: N818
    """The current plugin tried to read a flag absent from its declaration."""


class FlagDomainUnavailable(PluginFlagError):  # noqa: N818
    """The running process profile did not construct a declared flag's domain."""


_PLUGIN_FLAGS: dict[str, set[str]] = {}


def declare_flags(*keys: str) -> None:
    """Declare the core configuration flags the current plugin reads.

    Called at top level during a plugin's ``plugin.py`` import, which the
    loader wraps in ``PluginContext``. Every key is validated before this call
    changes the registry, so a malformed declaration leaves no partial record.

    Args:
        keys: Fully qualified ``<domain>.<field>`` core Settings keys.

    Raises:
        NoPluginContext: the declaration ran outside ``PluginContext``.
        UnknownFlag: a key is malformed, does not name a core field, or is sensitive.
    """
    plugin = _require_plugin_context("declare_flags")
    declared = {_validate_flag_key(key) for key in keys}
    if plugin not in _PLUGIN_FLAGS:
        _PLUGIN_FLAGS[plugin] = set()
    _PLUGIN_FLAGS[plugin].update(declared)


def read_flag(key: str) -> Any:
    """Return the effective turn-scoped value of a declared core configuration flag.

    Model-tuning fields use the framework's model-default layering. All other
    fields return their raw value from ``turn_settings``.

    Raises:
        NoPluginContext: the read ran outside ``PluginContext``.
        UndeclaredFlag: ``key`` is absent from the current plugin declaration.
        FlagDomainUnavailable: the current process profile lacks the key's domain.
    """
    plugin = _require_plugin_context("read_flag")
    if plugin not in _PLUGIN_FLAGS or key not in _PLUGIN_FLAGS[plugin]:
        raise UndeclaredFlag(
            f"plugin {plugin!r} cannot read flag {key!r}: declaration is contract; "
            "add it to declare_flags(...) first."
        )

    domain, field = key.split(".")
    from shared.config import settings
    from shared.config.turn_view import turn_settings

    if not settings.has_domain(domain):
        raise FlagDomainUnavailable(
            f"plugin {plugin!r} cannot read flag {key!r}: the {domain!r} domain "
            f"is unavailable in the {settings.profile!r} process profile."
        )

    explicit = getattr(getattr(turn_settings, domain), field)
    from shared.lm.registry import explain_setting, tuning_field_names

    if field in tuning_field_names():
        return explain_setting(
            field,
            model=turn_settings.lm.llm_model,
            explicit=explicit,
        ).value
    return explicit


def declared_flags(plugin: str) -> frozenset[str]:
    """Return a plugin's declared flags for tests and minimal introspection."""
    if plugin not in _PLUGIN_FLAGS:
        return frozenset()
    return frozenset(_PLUGIN_FLAGS[plugin])


def clear_plugin_flags() -> None:
    """Reset all plugin flag declarations during extension reload and test cleanup."""
    _PLUGIN_FLAGS.clear()


def _require_plugin_context(api: str) -> str:
    """Return the plugin currently importing or running, or raise the API-specific error."""
    plugin = current_plugin_name()
    if plugin is None:
        raise NoPluginContext(
            f"{api} must run inside PluginContext — the loader provides it during plugin import."
        )
    return plugin


def _validate_flag_key(key: str) -> str:
    """Validate one fully qualified, non-sensitive Settings key and return it."""
    if not isinstance(key, str) or key.count(".") != 1:
        raise UnknownFlag(f"unknown plugin flag {key!r}: flags must use exactly <domain>.<field>.")
    domain, field = key.split(".")
    if not domain or not field:
        raise UnknownFlag(f"unknown plugin flag {key!r}: flags must use exactly <domain>.<field>.")
    if domain not in _DOMAIN_ATTRS:
        raise UnknownFlag(f"unknown plugin flag {key!r}: {domain!r} is not a Settings domain.")

    fields = _fields()
    if field not in fields or fields[field].domain != domain:
        raise UnknownFlag(
            f"unknown plugin flag {key!r}: {field!r} is not a field in the {domain!r} domain."
        )
    ref = fields[field]
    if _field_is_sensitive(ref.info.json_schema_extra):
        raise UnknownFlag(f"unknown plugin flag {key!r}: secrets are not flags.")
    return key
