"""Desired roster persists independently of one transient start attempt."""

from pathlib import Path

import pytest

from shared import service_selection as selection


@pytest.fixture(autouse=True)
def isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(selection, "selection_path", lambda: tmp_path / "selection.json")


def test_allowlist_retains_and_excludes_future_plugins() -> None:
    assert selection.resolve_selection({"gateway", "ops"}, only=("gateway",)) == {"ops"}
    assert selection.resolve_selection({"gateway", "ops", "new-plugin"}) == {"ops", "new-plugin"}


def test_explicit_all_resets() -> None:
    selection.resolve_selection({"gateway", "ops"}, excluded=("ops",))
    assert selection.resolve_selection({"gateway", "ops"}) == {"ops"}
    assert selection.resolve_selection({"gateway", "ops"}, all_services=True) == set()


def test_internal_omission_never_changes_desired_state() -> None:
    selection.resolve_selection({"gateway", "ops", "frontend"}, excluded=("ops",))
    before = selection.selection_path().read_bytes()
    assert selection.resolve_selection(
        {"gateway", "ops", "frontend"}, excluded=("frontend",), persist=False
    ) == {"ops", "frontend"}
    assert selection.selection_path().read_bytes() == before


def test_unknown_and_conflicting_selections_refuse_before_write() -> None:
    with pytest.raises(ValueError, match="unknown"):
        selection.resolve_selection({"gateway"}, only=("typo",))
    with pytest.raises(ValueError, match="exclusive"):
        selection.resolve_selection({"gateway"}, only=("gateway",), all_services=True)
    assert not selection.selection_path().exists()


def test_invalid_durable_selection_never_means_all() -> None:
    selection.selection_path().write_text('{"version":2,"mode":"only","names":[]}')
    with pytest.raises(RuntimeError):
        selection.resolve_selection({"gateway"})


def test_preview_selection_does_not_publish_before_admission() -> None:
    selection.resolve_selection({"gateway", "ops"}, only=("gateway",))
    before = selection.selection_path().read_bytes()
    assert selection.resolve_selection({"gateway", "ops"}, all_services=True, publish=False) == set()
    assert selection.selection_path().read_bytes() == before
