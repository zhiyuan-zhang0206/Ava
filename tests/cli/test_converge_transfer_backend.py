"""Converge cross-machine-transfer step: probe the configured backend, warn not fail."""

from pathlib import Path

import pytest

import cli.commands._converge as cv


def _ctx(home: Path) -> cv.ConvergeCtx:
    return cv.ConvergeCtx(repo=Path("/repo"), ava_home=home, roles=frozenset({"agent-runner"}))


def test_passes_when_writable_drive_found(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    import shared.google_drive as gd

    monkeypatch.setattr(gd, "find_writable_google_drive", lambda: tmp_path / "My Drive")
    cv._ensure_cross_machine_transfer(_ctx(tmp_path))  # no raise


def test_warns_but_continues_when_drive_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A missing Drive backend must NOT block start: the probe warns on stderr and
    the runner keeps going (cross-machine transfer now degrades instead of failing)."""
    import shared.google_drive as gd

    monkeypatch.setattr(gd, "find_writable_google_drive", lambda: None)
    monkeypatch.setattr(gd, "candidate_drive_dirs", lambda: [Path("/some/My Drive")])
    cv._ensure_cross_machine_transfer(_ctx(tmp_path))  # no raise
    err = capsys.readouterr().err
    assert "no writable Google Drive synced folder" in err
    assert "will not work" in err


def test_skips_on_single_box(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A single box (also carries gateway) has no peer to transfer to — the probe
    must not even run (pinned: a probing implementation would touch the drive
    detection, and a blocking one would fail the start)."""
    import shared.google_drive as gd

    def _must_not_probe() -> Path:
        raise AssertionError("drive probe ran on a single box")

    monkeypatch.setattr(gd, "find_writable_google_drive", _must_not_probe)
    ctx = cv.ConvergeCtx(
        repo=Path("/repo"), ava_home=tmp_path, roles=frozenset({"gateway", "agent-runner"})
    )
    cv._ensure_cross_machine_transfer(ctx)  # no raise
    assert capsys.readouterr().err == ""


def test_skips_when_backend_none(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """AVA_CROSS_MACHINE_TRANSFER_BACKEND=none skips the probe on a split runner
    (pinned: no probe call, no warning)."""
    import shared.google_drive as gd
    from shared.config import settings

    def _must_not_probe() -> Path:
        raise AssertionError("drive probe ran with backend=none")

    monkeypatch.setattr(gd, "find_writable_google_drive", _must_not_probe)
    monkeypatch.setattr(settings.general, "cross_machine_transfer_backend", "none")
    cv._ensure_cross_machine_transfer(_ctx(tmp_path))  # no raise
    assert capsys.readouterr().err == ""


def test_step_registered_agent_runner_only() -> None:
    step = next(s for s in cv.CONVERGE_STEPS if s.name == "cross-machine transfer backend")
    assert step.roles == frozenset({"agent-runner"})
    assert step.requires_unit_config is True
