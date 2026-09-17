"""scripts/tcc-onboard-helper-grants.py: the tier model is data, and it stays honest.

The tier table is the machine-facing contract from design v1 (user decisions
2026-09-17): L2 is macmini's target, L3 gained Full Disk Access, and groups
whose trigger method is still pending verification must be named as pending
rather than silently skipped or attempted.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "tcc-onboard-helper-grants.py"
_spec = importlib.util.spec_from_file_location("tcc_onboard_under_test", _SCRIPT)
assert _spec is not None and _spec.loader is not None
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)


def test_l1_is_the_legacy_implemented_set() -> None:
    assert _mod.TIER_GROUPS["L1"] == _mod.IMPLEMENTED_GROUPS
    assert _mod.IMPLEMENTED_GROUPS == ("folders", "apple-events", "sr-ax")


def test_l0_is_empty_and_silent() -> None:
    assert _mod.TIER_GROUPS["L0"] == ()


def test_tiers_grow_monotonically_and_stay_known() -> None:
    l1, l2, l3 = (_mod.TIER_GROUPS[tier] for tier in ("L1", "L2", "L3"))
    assert set(l1) < set(l2) < set(l3)
    assert set(l3) <= set(_mod.ITEM_GROUPS)


def test_full_tier_carries_fda_and_the_extended_tier_does_not() -> None:
    assert "fda" in _mod.TIER_GROUPS["L3"]
    assert "fda" not in _mod.TIER_GROUPS["L2"]


def test_pending_groups_are_declared_with_a_reason() -> None:
    for group in ("appdata", "media", "icloud", "fda", "devtools"):
        assert group in _mod.PENDING_GROUPS
        assert _mod.PENDING_GROUPS[group]


def test_groups_for_tier_defaults_to_the_implemented_set() -> None:
    assert _mod.groups_for_tier(None) == _mod.IMPLEMENTED_GROUPS


def test_groups_for_tier_returns_the_table_entry() -> None:
    assert _mod.groups_for_tier("L2") == _mod.TIER_GROUPS["L2"]
    assert _mod.groups_for_tier("L0") == ()
