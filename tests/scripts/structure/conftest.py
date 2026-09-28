"""Shared isolation for the structure-gate tests."""

from __future__ import annotations

from dataclasses import replace

import pytest

from scripts.structure import locality


@pytest.fixture(autouse=True)
def _synthetic_decision_allowlists(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the real repository's single-owner exemptions out of synthetic roots.

    These tests point the gate at a temporary repository, where the real
    `DECISIONS[...].allowed` paths do not exist and would read as stale entries.
    A test about the allowlist itself installs its own `Decision`.
    """
    monkeypatch.setattr(
        locality,
        "DECISIONS",
        {name: replace(decision, allowed={}) for name, decision in locality.DECISIONS.items()},
    )
