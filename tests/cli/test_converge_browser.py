"""Converge browser step: sheds the legacy plugin file; preflight when enabled."""

from pathlib import Path

import pytest

import cli.commands._converge as cv


def _ctx(home: Path) -> cv.ConvergeCtx:
    return cv.ConvergeCtx(repo=Path("/repo"), ava_home=home, roles=frozenset({"agent-runner"}))


def _plugin_path(home: Path) -> Path:
    return home / "plugins" / "ava_chrome" / ".mcp.json"


def test_always_sheds_legacy_plugin_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The legacy converge-written file is removed (chrome is now built-in)."""
    monkeypatch.setattr(cv.settings.services, "browser_enabled", False)
    legacy = _plugin_path(tmp_path)
    legacy.parent.mkdir(parents=True)
    legacy.write_text("{}")
    cv._ensure_browser(_ctx(tmp_path))
    assert not legacy.exists()


def test_disabled_noop_when_already_absent(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(cv.settings.services, "browser_enabled", False)
    cv._ensure_browser(_ctx(tmp_path))  # no raise, nothing to remove
    assert not _plugin_path(tmp_path).exists()


def test_enabled_runs_preflight_writes_nothing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(cv.settings.services, "browser_enabled", True)
    import services.browser.profile as bp

    monkeypatch.setattr(cv, "browser_incapability", lambda: None)
    monkeypatch.setattr(bp, "ensure_browser_profile", lambda **_k: None)  # pyright: ignore[reportUnknownArgumentType]
    cv._ensure_browser(_ctx(tmp_path))
    assert not _plugin_path(tmp_path).exists()


def test_capable_offers_profile_seed_with_tty_flag(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """When the host is browser-capable, the step invokes the profile-seed offer,
    passing interactive = both stdin AND stdout are TTYs."""
    monkeypatch.setattr(cv.settings.services, "browser_enabled", True)
    import services.browser.profile as bp

    monkeypatch.setattr(cv, "browser_incapability", lambda: None)
    monkeypatch.setattr(cv.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(cv.sys.stdout, "isatty", lambda: False)
    seen: list[bool] = []
    monkeypatch.setattr(
        bp,
        "ensure_browser_profile",
        lambda *, interactive: seen.append(interactive),  # pyright: ignore[reportUnknownArgumentType]
    )
    cv._ensure_browser(_ctx(tmp_path))
    assert seen == [False]  # stdout not a tty -> not interactive


def test_incapable_host_does_not_offer_profile_seed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A headless host must return before ever offering the profile-seed choice."""
    monkeypatch.setattr(cv.settings.services, "browser_enabled", True)
    import services.browser.profile as bp

    monkeypatch.setattr(cv, "browser_incapability", lambda: "no display")

    def _must_not_call(**_k: object) -> None:
        raise AssertionError("profile seed offered on an incapable host")

    monkeypatch.setattr(bp, "ensure_browser_profile", _must_not_call)
    cv._ensure_browser(_ctx(tmp_path))  # warns + returns, no raise


def test_enabled_but_incapable_warns_not_raises(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """On a headless machine, _ensure_browser prints a warning and returns
    without raising — converge proceeds, browser is just skipped."""
    monkeypatch.setattr(cv.settings.services, "browser_enabled", True)
    monkeypatch.setattr(cv, "browser_incapability", lambda: "no display")
    # Must NOT raise — incapable hosts warn and return.
    cv._ensure_browser(_ctx(tmp_path))
    assert "ava-browser will not run on this host until this is fixed" in capsys.readouterr().err
    # Legacy plugin file is still shed.
    assert not _plugin_path(tmp_path).exists()


def test_step_registered_agent_runner_only() -> None:
    step = next(s for s in cv.CONVERGE_STEPS if s.name == "browser capability + plugin")
    assert step.roles == frozenset({"agent-runner"})
    assert step.requires_unit_config is True


def test_plugin_config_images_step_agent_runner_only() -> None:
    """Plugin config images are read only by agent processes (agent-runners); the
    step must not run on a gateway."""
    step = next(s for s in cv.CONVERGE_STEPS if s.name == "plugin config images")
    assert step.roles == frozenset({"agent-runner"})
    assert step.requires_unit_config is True
