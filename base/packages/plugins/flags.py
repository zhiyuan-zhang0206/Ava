"""Plugin access to declared, non-sensitive core configuration flags.

Plugins declare every core flag they read in their ``plugin.py`` ``contribute()``:

    def contribute() -> PluginContributions:
        return PluginContributions(
            flags=("agent.prompt_invest_future_enabled", "agent.agent_communication_style")
        )

The install (``ava.sdk_surface.install``) validates and records them through ``declare_flags``. Later,
plugin behavior identifies itself explicitly when it reads a declared flag from the current turn:

    from base.packages.plugins.flags import read_flag

    if read_flag("agent.prompt_invest_future_enabled", runtime.context.require_agent(), plugin="ava_fleet"):
        ...

Declaration is mandatory: ``read_flag`` rejects a key its identified plugin did
not declare. Keys are fully qualified as ``<domain>.<field>``. The namespace is
every non-sensitive core Settings field; secrets remain in their existing secret
channels and are never flags.

Reads take the turn's `AgentSlices`, so per-agent pins and the agent's model
apply. Model-tuning fields resolve with the same explicit-value, model-default,
then shared-default layering used by the framework; other fields return the
agent's value directly. A cluster config change takes effect on the next
process or agent start: values are read at start, not live.
"""

from collections.abc import Callable
from typing import Any

from base.host.env.agent_slices import AgentSlices
from base.host.env.config_registry import DOMAIN_ATTRS, fields
from base.packages.plugins.config_registration import _field_is_sensitive


class PluginFlagError(Exception):
    """Root of plugin core-flag declaration and read failures."""


class UnknownFlag(PluginFlagError):  # noqa: N818
    """A declared key is malformed, unknown, or names a sensitive core field."""


class UndeclaredFlag(PluginFlagError):  # noqa: N818
    """The current plugin tried to read a flag absent from its declaration."""


class FlagDomainUnavailable(PluginFlagError):  # noqa: N818
    """The running process profile did not construct a declared flag's domain."""


_PLUGIN_FLAGS: dict[str, set[str]] = {}


def declare_flags(plugin: str, keys: tuple[str, ...]) -> Callable[[], None]:
    """Record the core configuration flags `plugin` declared; returns the undo.

    Only the installer calls this. Every key is validated before the registry changes, so a malformed
    declaration leaves no partial record.

    Args:
        plugin: the declaring plugin.
        keys: Fully qualified ``<domain>.<field>`` core Settings keys.

    Raises:
        UnknownFlag: a key is malformed, does not name a core field, or is sensitive.
    """
    declared = {validate_flag_key(key) for key in keys}
    _PLUGIN_FLAGS.setdefault(plugin, set()).update(declared)

    def undo() -> None:
        _PLUGIN_FLAGS.pop(plugin, None)

    return undo


def read_flag(key: str, slices: AgentSlices, *, plugin: str) -> Any:
    """Return the effective value of a declared core configuration flag for the agent whose
    `slices` are given.

    Model-tuning fields use the framework's model-default layering. All other
    fields return their raw value for that agent (its pin, else the live default). The caller names
    its plugin explicitly.

    Args:
        key: Fully qualified ``<domain>.<field>`` core Settings key.
        slices: The turn's `AgentSlices` (a hook reads `runtime.context.require_agent()`).
        plugin: The reading plugin's name.

    Raises:
        UndeclaredFlag: ``key`` is absent from the identified plugin declaration.
        FlagDomainUnavailable: the current process profile lacks the key's domain.
    """
    plugin_name = plugin
    if plugin_name not in _PLUGIN_FLAGS or key not in _PLUGIN_FLAGS[plugin_name]:
        raise UndeclaredFlag(
            f"plugin {plugin_name!r} cannot read flag {key!r}: declaration is contract; "
            "add it to PluginContributions.flags first."
        )

    domain, field = key.split(".")
    from base.config import settings

    if not settings.has_domain(domain):
        raise FlagDomainUnavailable(
            f"plugin {plugin_name!r} cannot read flag {key!r}: the {domain!r} domain "
            f"is unavailable in the {settings.profile!r} process profile."
        )

    explicit = slices.read(domain, field)
    from base.lm.registry import explain_setting, tuning_field_names

    if field in tuning_field_names():
        return explain_setting(
            field,
            model=slices.brain.llm_model,
            explicit=explicit,
        ).value
    return explicit


def declared_flags(plugin: str) -> frozenset[str]:
    """Return a plugin's declared flags for tests and minimal introspection."""
    if plugin not in _PLUGIN_FLAGS:
        return frozenset()
    return frozenset(_PLUGIN_FLAGS[plugin])


def validate_flag_key(key: str) -> str:
    """Validate one fully qualified, non-sensitive Settings key and return it."""
    if not isinstance(key, str) or key.count(".") != 1:
        raise UnknownFlag(f"unknown plugin flag {key!r}: flags must use exactly <domain>.<field>.")
    domain, field = key.split(".")
    if not domain or not field:
        raise UnknownFlag(f"unknown plugin flag {key!r}: flags must use exactly <domain>.<field>.")
    if domain not in DOMAIN_ATTRS:
        raise UnknownFlag(f"unknown plugin flag {key!r}: {domain!r} is not a Settings domain.")

    field_refs = fields()
    if field not in field_refs or field_refs[field].domain != domain:
        raise UnknownFlag(
            f"unknown plugin flag {key!r}: {field!r} is not a field in the {domain!r} domain."
        )
    ref = field_refs[field]
    if _field_is_sensitive(ref.info.json_schema_extra):
        raise UnknownFlag(f"unknown plugin flag {key!r}: secrets are not flags.")
    return key
