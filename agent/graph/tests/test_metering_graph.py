from __future__ import annotations

import pytest

from meiva.sdk_surface import metering


def test_a_plugin_load_is_undone_by_the_autouse_teardown(request: pytest.FixtureRequest) -> None:
    import meiva.mcps
    from agent.state import plugin_state_schema
    from meiva.sdk_surface import install
    from meiva.sdk_surface.extensions import load_extensions
    from meiva.sdk_surface.installation import installed
    from meiva.sdk_surface.metering import is_recorder

    assert "_restore_metering" in request.fixturenames

    install.uninstall()
    bare_funnel = meiva.mcps.call_funnel.call_raw

    load_extensions(validate_state=plugin_state_schema)
    metered = {fq for p, a, fq in metering.instrument_targets() if is_recorder(getattr(p, a))}
    assert metered, "the leak this guards is gone"
    assert is_recorder(meiva.mcps.call_funnel.call_raw)

    installation = installed()
    assert installation is not None
    metering.uninstall(installation.metered)
    assert meiva.mcps.call_funnel.call_raw is bare_funnel
    assert not [fq for p, a, fq in metering.instrument_targets() if is_recorder(getattr(p, a))]
