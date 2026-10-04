"""Plugin loads are undone by the autouse teardown and leave no namespace behind."""

from __future__ import annotations

import pytest

import ava
from ava.sdk_surface import metering


def test_a_plugin_load_is_undone_by_the_autouse_teardown(request: pytest.FixtureRequest) -> None:
    """Issue #83: `load_extensions()` meters the process-global `ava` singleton as a
    side effect and nothing used to put it back, so one plugin-loading test silently
    rewrote the callables every later test in that xdist worker saw.

    The autouse `_restore_metering` in `tests/fixtures/guards.py` is what closes that.
    It runs after this test body, where a self-test cannot observe it, so the two
    halves are pinned separately: the fixture is wired onto every test, and its one
    action reverses a *real* `load_extensions()` — not just the hand-built
    `install()` the test above covers.
    """
    import ava.mcps
    from agent.extensions import load_extensions
    from ava.sdk_surface import install
    from ava.sdk_surface.metering import is_recorder

    assert "_restore_metering" in request.fixturenames

    install.uninstall()  # clean baseline (also restores any leftover recorder ledger)
    bare_funnel = ava.mcps._call_raw

    load_extensions()
    metered = {fq for p, a, fq in metering._instrument_targets() if is_recorder(getattr(p, a))}
    assert metered, "the leak this guards is gone"
    assert is_recorder(ava.mcps._call_raw)

    installation = install.installed()
    assert installation is not None
    metering.uninstall(installation.metered)  # the fixture's action, made observable
    assert ava.mcps._call_raw is bare_funnel
    # The whole surface, not just the funnel: a later test asserting on identity or
    # on call counts through a wrapped path must see no recorder anywhere.
    assert not [fq for p, a, fq in metering._instrument_targets() if is_recorder(getattr(p, a))]


def test_a_plugin_load_leaves_no_namespace_behind(
    request: pytest.FixtureRequest,
) -> None:
    """The test above still left every plugin's SDK surface in its xdist worker. A later
    partial cleanup then saw a half-installed surface (CI shard 14/16 on PR #3513).

    The autouse `_restore_plugin_registrations` (`tests/fixtures/plugin_registrations.py`)
    closes that. Same split as above: the guard is wired onto every test, it sees
    a real `load_extensions()`, and its reset leaves namespaces and members
    removed together.
    """
    from agent.extensions import load_extensions
    from ava.sdk_surface import install
    from tests.fixtures.plugin_registrations import (
        drop_plugin_registrations,
        plugin_registrations_present,
    )

    assert "_restore_plugin_registrations" in request.fixturenames
    assert not plugin_registrations_present()

    load_extensions()
    assert plugin_registrations_present()
    installation = install.installed()
    assert installation is not None
    names = [ns.name for _p, c in installation.registry.plugins for ns in c.sdk_namespaces]
    assert "cwd" in names, "the leak this guards is gone"
    # The install stamps `_qualname` on each namespace module, which outlives the test.
    stamped = [vars(ava)[name] for name in names]
    assert all("_qualname" in vars(module) for module in stamped)

    drop_plugin_registrations()  # the guard's action, made observable
    assert not plugin_registrations_present()
    assert install.installed() is None
    assert not any(hasattr(ava, name) for name in names)
    assert not any("_qualname" in vars(module) for module in stamped)
