"""The plugin-registration guard (`tests/fixtures/plugin_registrations.py`) sees what a plugin load installs and drops all of it."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

import ava

_GATE_CHILD = Path(__file__).with_name("child_gate_without_agent_layer.py")


def test_a_surface_installed_without_the_agent_layer_is_seen() -> None:
    """A plugin load that never imports `agent.state` (the schedule runner's in-process script)
    still installs namespaces and members on the SDK surface. The gate must see them, or the
    fixture never drops them and they stay for the rest of the worker. A fresh interpreter
    guarantees the agent layer is absent."""
    result = subprocess.run(  # noqa: S603 - fixed interpreter and child script
        [sys.executable, str(_GATE_CHILD)], capture_output=True, text=True, timeout=60, check=False
    )
    assert result.returncode == 0, result.stderr


def test_a_plugin_load_leaves_no_namespace_behind(
    request: pytest.FixtureRequest,
) -> None:
    """A real `load_extensions()` leaves every plugin's SDK surface in its xdist worker, and a later
    partial cleanup then saw a half-installed surface (CI shard 14/16 on PR #3513).

    The autouse `_restore_plugin_registrations` (`tests/fixtures/plugin_registrations.py`)
    closes that: it is wired onto every test, it sees a real `load_extensions()`, and its reset
    leaves namespaces and members removed together.
    """
    from agent.state import plugin_state_schema
    from ava.sdk_surface.extensions import load_extensions
    from ava.sdk_surface.installation import installed
    from tests.fixtures.plugin_registrations import (
        drop_plugin_registrations,
        plugin_registrations_present,
    )

    assert "_restore_plugin_registrations" in request.fixturenames
    assert not plugin_registrations_present()

    load_extensions(validate_state=plugin_state_schema)
    assert plugin_registrations_present()
    installation = installed()
    assert installation is not None
    names = [ns.name for _p, c in installation.registry.plugins for ns in c.sdk_namespaces]
    assert "cwd" in names, "the leak this guards is gone"
    # The install stamps `_qualname` on each namespace module, which outlives the test.
    stamped = [vars(ava)[name] for name in names]
    assert all("_qualname" in vars(module) for module in stamped)

    drop_plugin_registrations()  # the guard's action, made observable
    assert not plugin_registrations_present()
    assert installed() is None
    assert not any(hasattr(ava, name) for name in names)
    assert not any("_qualname" in vars(module) for module in stamped)
