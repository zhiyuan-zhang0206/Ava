"""The failures plugin config registration, binding and overlay validation raise."""


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
