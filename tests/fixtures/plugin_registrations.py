"""Per-test isolation for the installed SDK surface, the one process-global a plugin load writes.

A full plugin load (`agent.extensions.load_extensions`, reached through the plugin catalog, a
test, or a lazy `ava.*` miss) installs the plugins' SDK surface into the `ava` module
(`ava.sdk_surface.install`): `ava.<namespace>` surfaces, members, wrap layers, skill sources.
Nothing used to put it back, so one plugin-loading test left the whole set in its xdist worker, and
a later test that cleaned up only part of it saw a half-installed surface (CI backend shard 14/16 on
PR #3513).

The guard below uninstalls it after any test that started with nothing installed and ended with an
installation. `uninstall` undoes every layer together, newest first, so namespaces and their wraps
can never go out of step, and it takes the SDK-usage recorder off first because that sits outermost
over plugin wraps. A test that starts with an installation already present is left alone: there is
no empty state to return to. Hooks, state, prompt sections and notes are declared values
(`PluginContributions`), not process-global registrations, so nothing of theirs needs resetting.
"""

from __future__ import annotations

import sys
from collections.abc import Iterator

import pytest


def plugin_registrations_present() -> bool:
    """Whether a plugin SDK surface is installed in this process."""
    install = sys.modules.get("ava.sdk_surface.install")
    return install is not None and install.installed() is not None


def drop_plugin_registrations() -> None:
    """Uninstall the plugin SDK surface (which takes the SDK-usage recorder off first)."""
    install = sys.modules.get("ava.sdk_surface.install")
    if install is not None:
        install.uninstall()


@pytest.fixture(autouse=True)
def _restore_plugin_registrations() -> Iterator[None]:
    started_empty = not plugin_registrations_present()
    yield
    if started_empty and plugin_registrations_present():
        drop_plugin_registrations()
