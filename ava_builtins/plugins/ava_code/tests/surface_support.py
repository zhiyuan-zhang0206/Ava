"""Test support: the ava_code plugin's declarations as the loader would install them."""

from base.packages.plugins.extensions import ExtensionRegistry, PluginContributions


def code_registry(*others: tuple[str, PluginContributions]) -> ExtensionRegistry:
    """ava_code (surface and runtime faces merged), followed by any further plugins."""
    from ava_builtins.plugins.ava_code import agent_runtime, plugin

    own = plugin.contribute().merged(agent_runtime.contribute())
    return ExtensionRegistry((("ava_code", own), *others))
