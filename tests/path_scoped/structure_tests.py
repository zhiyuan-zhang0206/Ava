"""Shared isolation for the structure-gate tests (registered by `tests/fixtures/path_scopes.py`)."""

from __future__ import annotations

from dataclasses import replace

import pytest

from scripts.structure import locality
from scripts.structure.ambient_state import allowlist as ambient_allowlist


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


@pytest.fixture(autouse=True)
def _synthetic_ambient_allowlists(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the real repository's ambient-state lists out of synthetic roots, for the same
    reason: their paths and sites do not exist in a temporary repository and would read as
    stale. A test about a list installs its own entries."""
    for name in (
        "SINK_FACADES",
        "ALLOWED",
        "DEFERRED",
        "PURE_REPO_CALLEES",
        "SLICED_PACKAGES",
        "DB_HANDLE_PACKAGES",
        "ENDPOINT_PACKAGES",
        "BUS_PACKAGES",
    ):
        monkeypatch.setattr(ambient_allowlist, name, {})
