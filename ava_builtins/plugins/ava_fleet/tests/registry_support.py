"""Test support: the registry the loader would build with only the fleet plugin enabled."""

from base.packages.plugins.extensions import ExtensionRegistry


def fleet_registry() -> ExtensionRegistry:
    from ava_builtins.plugins.ava_fleet import agent_runtime

    return ExtensionRegistry((("ava_fleet", agent_runtime.contribute()),))
