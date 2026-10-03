"""Test support: the registry the loader would build with only the fleet plugin enabled."""

from collections.abc import Generator
from contextlib import contextmanager

from base.packages.plugins.extensions import ExtensionRegistry


def fleet_registry() -> ExtensionRegistry:
    from ava_builtins.plugins.ava_fleet import agent_runtime

    return ExtensionRegistry((("ava_fleet", agent_runtime.contribute()),))


@contextmanager
def installed_fleet_surface() -> Generator[ExtensionRegistry]:
    """The fleet plugin's SDK surface installed into `ava` for the duration of the block."""
    from ava.sdk_surface import install
    from ava_builtins.plugins.ava_fleet import agent_runtime, plugin

    contributions = plugin.contribute().merged(agent_runtime.contribute())
    admitted = install.install(ExtensionRegistry((("ava_fleet", contributions),)))
    try:
        yield admitted
    finally:
        install.uninstall()
