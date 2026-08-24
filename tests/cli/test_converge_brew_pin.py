"""Converge's warning-only assertion for manually pinned Homebrew formulae."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

import cli.commands._converge as cv
import cli.commands._converge_brew_pin as cbp
from shared import brew_pin

EXPECTED_PINNED_FORMULAE = frozenset(
    {
        "ca-certificates",
        "cloudflared",
        "grafana",
        "json-c",
        "node",
        "openssl@3",
        "pgbouncer",
        "postgresql@17",
        "redis",
        "redis@8.2",
        "tailscale",
        "uv",
    }
)


def _ctx(tmp_path: Path) -> cv.ConvergeCtx:
    return cv.ConvergeCtx(repo=Path("/repo"), ava_home=tmp_path, roles=cv.ALL_ROLES)


def _brew_output(monkeypatch: pytest.MonkeyPatch, formulae: set[str]) -> None:
    def fake_run(args: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        assert args == ["brew", "list", "--pinned"]
        return subprocess.CompletedProcess(args, 0, stdout="\n".join(sorted(formulae)), stderr="")

    monkeypatch.setattr(brew_pin.subprocess, "run", fake_run)


def test_pinned_formula_manifest_is_exact() -> None:
    assert brew_pin.PINNED_BREW_FORMULAE == EXPECTED_PINNED_FORMULAE


def test_step_is_registered_after_firewall_for_both_roles() -> None:
    steps = cv.CONVERGE_STEPS
    brew_pin_index = next(i for i, step in enumerate(steps) if step.apply is cbp.ensure_brew_pin)
    firewall_index = next(
        i for i, step in enumerate(steps) if step.apply.__name__ == "ensure_firewall_allowlist"
    )
    step = steps[brew_pin_index]
    assert brew_pin_index == firewall_index + 1
    assert step.roles == cv.ALL_ROLES
    assert step.requires_unit_config is False


def test_all_formulae_pinned_is_silent(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(cbp, "IS_MACOS", True)
    _brew_output(monkeypatch, set(EXPECTED_PINNED_FORMULAE))

    cbp.ensure_brew_pin(_ctx(tmp_path))

    assert capsys.readouterr().err == ""


def test_missing_formula_warns_with_manual_repin_command(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(cbp, "IS_MACOS", True)
    _brew_output(monkeypatch, set(EXPECTED_PINNED_FORMULAE - {"redis@8.2"}))

    cbp.ensure_brew_pin(_ctx(tmp_path))

    warning = capsys.readouterr().err
    assert warning.startswith("  ! brew-pin:")
    assert "redis@8.2" in warning
    assert "brew pin redis@8.2" in warning


def test_brew_absent_is_silent(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(cbp, "IS_MACOS", True)

    def absent(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        raise FileNotFoundError("brew")

    monkeypatch.setattr(brew_pin.subprocess, "run", absent)

    cbp.ensure_brew_pin(_ctx(tmp_path))

    assert capsys.readouterr().err == ""


def test_non_macos_is_silent_without_calling_brew(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(cbp, "IS_MACOS", False)
    monkeypatch.setattr(
        cbp,
        "unpinned_formulae",
        lambda: pytest.fail("non-macOS converge must not invoke brew"),
    )

    cbp.ensure_brew_pin(_ctx(tmp_path))

    assert capsys.readouterr().err == ""
