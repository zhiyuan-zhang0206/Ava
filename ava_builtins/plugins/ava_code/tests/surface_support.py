from base.packages.plugins.extensions import ExtensionRegistry, PluginContributions


def code_registry(*others: tuple[str, PluginContributions]) -> ExtensionRegistry:
    from ava_builtins.plugins.ava_code import agent_runtime, plugin

    own = plugin.contribute().merged(agent_runtime.contribute())
    return ExtensionRegistry((("ava_code", own), *others))
