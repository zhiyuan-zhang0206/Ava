"""Converge google-drive step: fail fast when no writable Drive folder is found."""

from pathlib import Path

import pytest

import cli.commands._converge as cv


def _ctx(home: Path) -> cv.ConvergeCtx:
    return cv.ConvergeCtx(repo=Path("/repo"), ava_home=home, roles=frozenset({"agent-runner"}))


def test_passes_when_writable_drive_found(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    import shared.google_drive as gd

    monkeypatch.setattr(gd, "find_writable_google_drive", lambda: tmp_path / "My Drive")
    cv._ensure_google_drive(_ctx(tmp_path))  # no raise


def test_raises_when_no_writable_drive(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    import shared.google_drive as gd

    monkeypatch.setattr(gd, "find_writable_google_drive", lambda: None)
    monkeypatch.setattr(gd, "candidate_drive_dirs", lambda: [Path("/some/My Drive")])
    with pytest.raises(RuntimeError, match="no writable Google Drive synced folder"):
        cv._ensure_google_drive(_ctx(tmp_path))


def test_skips_on_single_box(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A single box (also carries gateway) has no peer to transfer to — skip the gate."""
    import shared.google_drive as gd

    monkeypatch.setattr(gd, "find_writable_google_drive", lambda: None)
    ctx = cv.ConvergeCtx(
        repo=Path("/repo"), ava_home=tmp_path, roles=frozenset({"gateway", "agent-runner"})
    )
    cv._ensure_google_drive(ctx)  # no raise


def test_skips_when_opt_out(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """AVA_REQUIRE_GOOGLE_DRIVE=false opts a split runner out of the gate."""
    import shared.google_drive as gd
    from shared.config import settings

    monkeypatch.setattr(gd, "find_writable_google_drive", lambda: None)
    monkeypatch.setattr(settings.general, "require_google_drive", False)
    cv._ensure_google_drive(_ctx(tmp_path))  # no raise


def test_step_registered_agent_runner_only() -> None:
    step = next(s for s in cv.CONVERGE_STEPS if s.name == "google drive sync area")
    assert step.roles == frozenset({"agent-runner"})
    assert step.requires_unit_config is True
