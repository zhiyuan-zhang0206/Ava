"""Plugin loads are undone by the autouse teardown and leave no namespace behind."""

from __future__ import annotations

import pytest

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
    from agent.state import plugin_state_schema
    from ava.sdk_surface import install
    from ava.sdk_surface.extensions import load_extensions
    from ava.sdk_surface.installation import installed
    from ava.sdk_surface.metering import is_recorder

    assert "_restore_metering" in request.fixturenames

    install.uninstall()  # clean baseline (also restores any leftover recorder ledger)
    bare_funnel = ava.mcps.call_funnel.call_raw

    load_extensions(validate_state=plugin_state_schema)
    metered = {fq for p, a, fq in metering.instrument_targets() if is_recorder(getattr(p, a))}
    assert metered, "the leak this guards is gone"
    assert is_recorder(ava.mcps.call_funnel.call_raw)

    installation = installed()
    assert installation is not None
    metering.uninstall(installation.metered)  # the fixture's action, made observable
    assert ava.mcps.call_funnel.call_raw is bare_funnel
    # The whole surface, not just the funnel: a later test asserting on identity or
    # on call counts through a wrapped path must see no recorder anywhere.
    assert not [fq for p, a, fq in metering.instrument_targets() if is_recorder(getattr(p, a))]
