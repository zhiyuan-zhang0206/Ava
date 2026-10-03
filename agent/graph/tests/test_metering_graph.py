"""Plugin loads are undone by the autouse teardown and leave no section behind their namespace."""

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
    from agent.graph import _build
    from ava.sdk_surface.metering import _RECORDERS

    assert "_restore_metering" in request.fixturenames

    metering.uninstall()
    bare_funnel = ava.mcps._call_raw

    _build.load_extensions()
    metered = {fq for p, a, fq in metering._instrument_targets() if getattr(p, a) in _RECORDERS}
    assert metered, "the leak this guards is gone"
    assert ava.mcps._call_raw in _RECORDERS

    metering.uninstall()  # the fixture's action, made observable
    assert ava.mcps._call_raw is bare_funnel
    # The whole surface, not just the funnel: a later test asserting on identity or
    # on call counts through a wrapped path must see no recorder anywhere.
    assert not [fq for p, a, fq in metering._instrument_targets() if getattr(p, a) in _RECORDERS]


def test_a_plugin_load_leaves_no_section_behind_its_namespace(
    request: pytest.FixtureRequest,
) -> None:
    """The test above still left every plugin registration in its xdist worker. A later
    namespace-only cleanup (`ava.clear_registered_namespaces()`) then stranded the
    ava_code prompt section without `ava.cwd`, and every `build_system_prompt()` after
    it raised `module 'ava' has no attribute 'cwd'` (CI shard 14/16 on PR #3513).

    The autouse `_restore_plugin_registrations` (`tests/fixtures/plugin_registrations.py`)
    closes that. Same split as above: the guard is wired onto every test, it sees
    a real `load_extensions()`, and its reset leaves sections, namespaces and
    state fields empty together.
    """
    from agent.graph import _build
    from agent.graph.system_prompt import _FRAMEWORK_SECTION_COUNT, _SYSTEM_PROMPT_SECTIONS
    from agent.state import _EXTRA_FIELDS
    from ava.sdk_surface.plugins import _REGISTERED_MEMBERS, _REGISTERED_NAMESPACES
    from tests.fixtures.plugin_registrations import (
        drop_plugin_registrations,
        plugin_registrations_present,
    )

    assert "_restore_plugin_registrations" in request.fixturenames
    assert not plugin_registrations_present()

    _build.load_extensions()
    assert plugin_registrations_present()
    assert "cwd" in _REGISTERED_NAMESPACES, "the leak this guards is gone"
    # `register_namespace` stamps `_qualname` on the namespace module, which outlives the test.
    stamped = [vars(ava)[name] for name in _REGISTERED_NAMESPACES]
    assert all("_qualname" in vars(module) for module in stamped)

    drop_plugin_registrations()  # the guard's action, made observable
    assert not plugin_registrations_present()
    assert len(_SYSTEM_PROMPT_SECTIONS) == _FRAMEWORK_SECTION_COUNT
    assert not _REGISTERED_NAMESPACES and not _REGISTERED_MEMBERS and not _EXTRA_FIELDS
    assert not hasattr(ava, "cwd")
    assert not any("_qualname" in vars(module) for module in stamped)
