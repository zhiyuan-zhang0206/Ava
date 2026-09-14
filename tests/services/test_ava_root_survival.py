"""services.ava_root.survival: the update-survival roster skeleton (G6a(2))."""

from __future__ import annotations

import pytest

from services.ava_root import survival
from services.ava_root.survival import UPDATE_SURVIVAL_UNIT_IDS, is_update_survival_unit


def test_default_roster_is_empty_and_immutable() -> None:
    assert isinstance(UPDATE_SURVIVAL_UNIT_IDS, frozenset)
    assert not UPDATE_SURVIVAL_UNIT_IDS
    assert is_update_survival_unit("gateway") is False


def test_predicate_reads_the_roster(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(survival, "UPDATE_SURVIVAL_UNIT_IDS", frozenset({"gateway", "lgtm"}))
    assert is_update_survival_unit("gateway") is True
    assert is_update_survival_unit("lgtm") is True
    assert is_update_survival_unit("worker") is False
