"""Home ownership is resolved before first-start identity can write anything."""

from pathlib import Path

import pytest

from cli import start_intent


def test_dev_checkout_cannot_initialize_default_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    user = tmp_path / "user"
    monkeypatch.setattr(Path, "home", lambda: user)
    monkeypatch.setattr(start_intent, "_checkout", lambda: tmp_path / "dev")
    monkeypatch.setenv("AVA_HOME", str(user / ".ava"))
    with pytest.raises(ValueError, match="production home"):
        start_intent._home(worktree=False)
    assert not user.exists()


def test_unanchored_checkout_does_not_select_production(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "user")
    monkeypatch.setattr(start_intent, "_checkout", lambda: tmp_path / "dev")
    monkeypatch.delenv("AVA_HOME", raising=False)
    with pytest.raises(ValueError, match="unanchored"):
        start_intent._home(worktree=False)


def test_conflicting_pointer_is_not_overwritten(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    pointer = checkout / ".ava_home"
    pointer.write_text(str(tmp_path / "first") + "\n")
    monkeypatch.setattr(start_intent, "_checkout", lambda: checkout)
    monkeypatch.setenv("AVA_HOME", str(tmp_path / "second"))
    with pytest.raises(ValueError, match="contradicts"):
        start_intent._home(worktree=True)
    assert pointer.read_text().strip() == str(tmp_path / "first")
