"""Converge browser step: sheds the legacy plugin file; preflight when enabled."""

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

import cli.commands.converge.host as cv
from base.config import ConfigBoot
from base.telemetry import EventPipeline
from tests.path_scoped.cli_tests import operator_database as operator_database
from tests.path_scoped.cli_tests import operator_pipeline as operator_pipeline


def _ctx(
    home: Path, *, operator_database: Callable[[], Any], producer: Callable[[], EventPipeline]
) -> cv.ConvergeCtx:
    return cv.ConvergeCtx(
        repo=Path("/repo"),
        ava_home=home,
        roles=frozenset({"agent-runner"}),
        config=ConfigBoot(),
        database_factory=operator_database,
        producer=producer,
    )


def _plugin_path(home: Path) -> Path:
    return home / "plugins" / "ava_chrome" / ".mcp.json"


def test_always_sheds_legacy_plugin_file(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    operator_database: Callable[[], Any],
    operator_pipeline: Callable[[], EventPipeline],
) -> None:
    """The legacy converge-written file is removed (chrome is now built-in)."""
    ctx = _ctx(tmp_path, operator_database=operator_database, producer=operator_pipeline)
    ctx.config.set_field("browser_enabled", False)
    legacy = _plugin_path(tmp_path)
    legacy.parent.mkdir(parents=True)
    legacy.write_text("{}")
    cv._ensure_browser(ctx)
    assert not legacy.exists()


def test_disabled_noop_when_already_absent(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    operator_database: Callable[[], Any],
    operator_pipeline: Callable[[], EventPipeline],
) -> None:
    ctx = _ctx(tmp_path, operator_database=operator_database, producer=operator_pipeline)
    ctx.config.set_field("browser_enabled", False)
    cv._ensure_browser(ctx)  # no raise, nothing to remove
    assert not _plugin_path(tmp_path).exists()


def test_enabled_runs_preflight_writes_nothing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    operator_database: Callable[[], Any],
    operator_pipeline: Callable[[], EventPipeline],
) -> None:
    ctx = _ctx(tmp_path, operator_database=operator_database, producer=operator_pipeline)
    ctx.config.set_field("browser_enabled", True)
    import services.desktop.browser.profile as bp

    monkeypatch.setattr(cv, "browser_incapability", lambda: None)
    monkeypatch.setattr(bp, "ensure_browser_profile", lambda **_k: None)  # pyright: ignore[reportUnknownArgumentType]
    cv._ensure_browser(ctx)
    assert not _plugin_path(tmp_path).exists()


def test_capable_offers_profile_seed_with_tty_flag(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    operator_database: Callable[[], Any],
    operator_pipeline: Callable[[], EventPipeline],
) -> None:
    """When the host is browser-capable, the step invokes the profile-seed offer,
    passing interactive = both stdin AND stdout are TTYs."""
    ctx = _ctx(tmp_path, operator_database=operator_database, producer=operator_pipeline)
    ctx.config.set_field("browser_enabled", True)
    import services.desktop.browser.profile as bp

    monkeypatch.setattr(cv, "browser_incapability", lambda: None)
    monkeypatch.setattr(cv.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(cv.sys.stdout, "isatty", lambda: False)
    seen: list[bool] = []
    monkeypatch.setattr(
        bp,
        "ensure_browser_profile",
        lambda *, interactive: seen.append(interactive),  # pyright: ignore[reportUnknownArgumentType]
    )
    cv._ensure_browser(ctx)
    assert seen == [False]  # stdout not a tty -> not interactive


def test_incapable_host_does_not_offer_profile_seed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    operator_database: Callable[[], Any],
    operator_pipeline: Callable[[], EventPipeline],
) -> None:
    """A headless host must return before ever offering the profile-seed choice."""
    ctx = _ctx(tmp_path, operator_database=operator_database, producer=operator_pipeline)
    ctx.config.set_field("browser_enabled", True)
    import services.desktop.browser.profile as bp

    monkeypatch.setattr(cv, "browser_incapability", lambda: "no display")

    def _must_not_call(**_k: object) -> None:
        raise AssertionError("profile seed offered on an incapable host")

    monkeypatch.setattr(bp, "ensure_browser_profile", _must_not_call)
    cv._ensure_browser(ctx)  # warns + returns, no raise


def test_enabled_but_incapable_warns_not_raises(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    operator_database: Callable[[], Any],
    operator_pipeline: Callable[[], EventPipeline],
) -> None:
    """On a headless machine, converge prints an informational skip and returns."""
    ctx = _ctx(tmp_path, operator_database=operator_database, producer=operator_pipeline)
    ctx.config.set_field("browser_enabled", True)
    monkeypatch.setattr(cv, "browser_incapability", lambda: "no display")
    cv._ensure_browser(ctx)
    stderr = capsys.readouterr().err
    assert "not applicable" in stderr
    assert "Install Node.js" not in stderr
    # Legacy plugin file is still shed.
    assert not _plugin_path(tmp_path).exists()


def test_enabled_with_missing_npx_prints_the_repair_box(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    operator_database: Callable[[], Any],
    operator_pipeline: Callable[[], EventPipeline],
) -> None:
    """A repairable dependency gap keeps converge's existing warn-not-raise path."""
    ctx = _ctx(tmp_path, operator_database=operator_database, producer=operator_pipeline)
    ctx.config.set_field("browser_enabled", True)
    monkeypatch.setattr(
        cv, "browser_incapability", lambda: "no npx (install Node.js for chrome-devtools-mcp)"
    )
    cv._ensure_browser(ctx)
    assert "Install Node.js" in capsys.readouterr().err


def test_steps_read_their_context_owner_and_later_overlay(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    operator_database: Callable[[], Any],
    operator_pipeline: Callable[[], EventPipeline],
) -> None:
    from services.desktop.browser import profile

    first = _ctx(
        tmp_path / "first", operator_database=operator_database, producer=operator_pipeline
    )
    second = _ctx(
        tmp_path / "second", operator_database=operator_database, producer=operator_pipeline
    )
    first.config.set_field("browser_enabled", False)
    second.config.set_field("browser_enabled", True)
    probed: list[bool] = []

    def capable() -> None:
        probed.append(True)

    def seed(*, interactive: bool) -> None:
        assert isinstance(interactive, bool)

    monkeypatch.setattr(cv, "browser_incapability", capable)
    monkeypatch.setattr(profile, "ensure_browser_profile", seed)
    cv._ensure_browser(first)
    cv._ensure_browser(second)
    assert probed == [True]
    assert first.config is not second.config
    first.config.set_field("browser_enabled", True)
    cv._ensure_browser(first)
    assert probed == [True, True]


def test_context_read_failure_keeps_the_original_error(
    tmp_path: Path,
    operator_database: Callable[[], Any],
    operator_pipeline: Callable[[], EventPipeline],
) -> None:
    error = RuntimeError("context configuration is unavailable")

    class FailedBoot(ConfigBoot):
        def read_process_environment(self) -> None:
            raise error

    ctx = cv.ConvergeCtx(
        repo=tmp_path,
        ava_home=tmp_path,
        roles=frozenset({"agent-runner"}),
        config=FailedBoot(),
        database_factory=operator_database,
        producer=operator_pipeline,
    )
    with pytest.raises(RuntimeError) as caught:
        cv._ensure_browser(ctx)
    assert caught.value is error


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
