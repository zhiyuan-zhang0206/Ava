import sys
from types import SimpleNamespace

from ava.sdk_surface import install
from base.packages.plugins.extensions import (
    ExtensionRegistry,
    PluginContributions,
    SdkNamespace,
)
from tests.fixtures.plugin_registrations import plugin_registrations_present

assert "agent.state" not in sys.modules, "the fresh interpreter must start without the agent layer"
assert not plugin_registrations_present()
install.install(
    ExtensionRegistry(
        (
            (
                "probe",
                PluginContributions(
                    sdk_namespaces=(SdkNamespace("gate_probe", SimpleNamespace()),)
                ),
            ),
        )
    )
)
assert plugin_registrations_present()
assert "agent.state" not in sys.modules, "asking must not load the agent layer"
