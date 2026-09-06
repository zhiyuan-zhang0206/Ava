"""Runtime detection of drift from the manually approved Homebrew pin set."""

from __future__ import annotations

import logging
import subprocess

import pytest

from services.healthchecks import brew_pin as hc
from shared import brew_pin


@pytest.fixture(autouse=True)
def _macos_check(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(hc, "IS_MACOS", True)
    monkeypatch.setattr(hc, "init_gateway_process", lambda *_args, **_kwargs: None)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(hc, "_reported_missing", ())


def _brew_output(
    monkeypatch: pytest.MonkeyPatch,
    pinned: set[str],
    installed: set[str] | None = None,
) -> None:
    installed_formulae = set(brew_pin.PINNED_BREW_FORMULAE) if installed is None else installed

    def fake_run(args: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        if args == ["brew", "list", "--pinned"]:
            formulae = pinned
        elif args == ["brew", "list", "--formula"]:
            formulae = installed_formulae
        else:
            raise AssertionError(f"unexpected brew command: {args!r}")
        return subprocess.CompletedProcess(args, 0, stdout="\n".join(sorted(formulae)), stderr="")

    monkeypatch.setattr(brew_pin.subprocess, "run", fake_run)


def _error_records(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [
        record
        for record in caplog.records
        if record.name == "services.healthchecks.brew_pin" and record.levelno == logging.ERROR
    ]


def test_unpinned_formula_logs_once_per_episode(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    _brew_output(monkeypatch, set(brew_pin.PINNED_BREW_FORMULAE - {"redis@8.2"}))

    with caplog.at_level(logging.ERROR, logger="services.healthchecks.brew_pin"):
        hc.main()
        hc.main()

    records = _error_records(caplog)
    assert len(records) == 1
    assert "redis@8.2" in records[0].getMessage()


def test_pinned_round_is_silent_and_resets_episode(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    unpinned = set(brew_pin.PINNED_BREW_FORMULAE - {"redis@8.2"})
    _brew_output(monkeypatch, unpinned)
    with caplog.at_level(logging.ERROR, logger="services.healthchecks.brew_pin"):
        hc.main()

    _brew_output(monkeypatch, set(brew_pin.PINNED_BREW_FORMULAE))
    hc.main()
    assert hc._reported_missing == ()
    assert len(_error_records(caplog)) == 1

    _brew_output(monkeypatch, unpinned)
    hc.main()
    assert len(_error_records(caplog)) == 2


def test_brew_absent_is_silent(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    def absent(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        raise FileNotFoundError("brew")

    monkeypatch.setattr(brew_pin.subprocess, "run", absent)

    with caplog.at_level(logging.ERROR, logger="services.healthchecks.brew_pin"):
        hc.main()

    assert _error_records(caplog) == []


def test_non_macos_is_silent_without_calling_brew(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(hc, "IS_MACOS", False)
    monkeypatch.setattr(
        hc,
        "unpinned_formulae",
        lambda: pytest.fail("non-macOS healthcheck must not invoke brew"),
    )

    with caplog.at_level(logging.ERROR, logger="services.healthchecks.brew_pin"):
        hc.main()

    assert _error_records(caplog) == []
