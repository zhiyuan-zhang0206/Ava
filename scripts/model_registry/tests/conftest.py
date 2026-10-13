"""Consumer-local opt-in fixtures for this test directory."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from base.host.net.tests.retry_waits import retry_waits as retry_waits
from scripts.model_registry import check_model_updates


@pytest.fixture
def bare_tracker(monkeypatch: pytest.MonkeyPatch) -> Any:
    """The tracker script module with its provider-reply DoH cache emptied for the test."""
    monkeypatch.setattr(check_model_updates, "doh_cache", {})
    return check_model_updates


@pytest.fixture(name="tracker")
def _tracker_with_empty_catalog(bare_tracker: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    """The tracker with an explicit test-owned comparison registry.

    The CLI composition root builds a catalog from the host's provider plugins;
    these tests replace that constructor with an empty registry.
    """
    monkeypatch.setattr(bare_tracker, "build_model_catalog", lambda: SimpleNamespace(models={}))
    return bare_tracker
