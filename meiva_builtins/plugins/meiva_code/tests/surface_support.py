from base.packages.plugins.extensions import ExtensionRegistry, PluginContributions


def code_registry(*others: tuple[str, PluginContributions]) -> ExtensionRegistry:
    from meiva_builtins.plugins.meiva_code import agent_runtime, plugin

    own = plugin.contribute().merged(agent_runtime.contribute())
    return ExtensionRegistry((("meiva_code", own), *others))
