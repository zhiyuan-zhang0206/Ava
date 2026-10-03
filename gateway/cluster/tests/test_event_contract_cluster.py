"""The cluster ops series consume the contract's grid constants instead of re-declaring them."""

from __future__ import annotations

from base.events.contract import OPS_BUCKET_S, OPS_GRID_ORIGIN


def test_grid_constants_are_single_definitions() -> None:
    assert OPS_BUCKET_S == 60
    assert OPS_GRID_ORIGIN.isoformat() == "2000-01-01T00:00:00+00:00"
    # the consumers must import from the contract, not re-declare
    from gateway.cluster.ops_series import _GRID_ORIGIN as _LG_GRID

    assert _LG_GRID == OPS_GRID_ORIGIN
