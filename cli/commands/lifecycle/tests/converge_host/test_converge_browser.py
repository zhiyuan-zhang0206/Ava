"""Converge browser step: preflight when enabled."""

from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

import cli.commands.lifecycle.converge_host as cv
from base.cluster.machine.boot import machine_from_boot
from base.config import ConfigBoot
from base.config.tests.configuration import snapshot_process_config
from base.telemetry import EventPipeline
from cli.database.runtime import CommandRuntime
from cli.database.tests.command_inputs import command_runtime as command_runtime
from tests.path_scoped.cli_tests import operator_database as operator_database
from tests.path_scoped.cli_tests import operator_pipeline as operator_pipeline


def _ctx(
    home: Path,
    *,
    operator_database: Callable[[], Any],
    producer: Callable[[], EventPipeline],
    command_runtime: CommandRuntime,
) -> cv.ConvergeCtx:
    return cv.ConvergeCtx(
        repo=Path("/repo"),
        ava_home=home,
        roles=frozenset({"agent-runner"}),
        config=command_runtime.config,
        database_factory=operator_database,
        producer=producer,
        machine=command_runtime.machine,
    )


def test_enabled_runs_preflight(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    operator_database: Callable[[], Any],
    operator_pipeline: Callable[[], EventPipeline],
    *,
    command_runtime: CommandRuntime,
) -> None:
    ctx = _ctx(
        tmp_path,
        operator_database=operator_database,
        producer=operator_pipeline,
        command_runtime=command_runtime,
    )
    ctx.config.set_field("browser_enabled", True)
    import services.desktop.browser.profile as bp

    monkeypatch.setattr(cv, "browser_incapability", lambda _chrome: None)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(bp, "ensure_browser_profile", lambda **_k: None)  # pyright: ignore[reportUnknownArgumentType]
    cv.ensure_browser(ctx)


def test_capable_offers_profile_seed_with_tty_flag(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    operator_database: Callable[[], Any],
    operator_pipeline: Callable[[], EventPipeline],
    *,
    command_runtime: CommandRuntime,
) -> None:
    """When the host is browser-capable, the step invokes the profile-seed offer,
    passing interactive = both stdin AND stdout are TTYs."""
    ctx = _ctx(
        tmp_path,
        operator_database=operator_database,
        producer=operator_pipeline,
        command_runtime=command_runtime,
    )
    ctx.config.set_field("browser_enabled", True)
    import services.desktop.browser.profile as bp

    monkeypatch.setattr(cv, "browser_incapability", lambda _chrome: None)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(cv.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(cv.sys.stdout, "isatty", lambda: False)
    seen: list[bool] = []
    monkeypatch.setattr(
        bp,
        "ensure_browser_profile",
        lambda *, interactive: seen.append(interactive),  # pyright: ignore[reportUnknownArgumentType]
    )
    cv.ensure_browser(ctx)
    assert seen == [False]  # stdout not a tty -> not interactive


def test_incapable_host_does_not_offer_profile_seed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    operator_database: Callable[[], Any],
    operator_pipeline: Callable[[], EventPipeline],
    *,
    command_runtime: CommandRuntime,
) -> None:
    """A headless host must return before ever offering the profile-seed choice."""
    ctx = _ctx(
        tmp_path,
        operator_database=operator_database,
        producer=operator_pipeline,
        command_runtime=command_runtime,
    )
    ctx.config.set_field("browser_enabled", True)
    import services.desktop.browser.profile as bp

    monkeypatch.setattr(cv, "browser_incapability", lambda _chrome: "no display")  # pyright: ignore[reportUnknownArgumentType]

    def _must_not_call(**_k: object) -> None:
        raise AssertionError("profile seed offered on an incapable host")

    monkeypatch.setattr(bp, "ensure_browser_profile", _must_not_call)
    cv.ensure_browser(ctx)  # warns + returns, no raise


def test_enabled_but_incapable_warns_not_raises(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    operator_database: Callable[[], Any],
    operator_pipeline: Callable[[], EventPipeline],
    *,
    command_runtime: CommandRuntime,
) -> None:
    """On a headless machine, converge prints an informational skip and returns."""
    ctx = _ctx(
        tmp_path,
        operator_database=operator_database,
        producer=operator_pipeline,
        command_runtime=command_runtime,
    )
    ctx.config.set_field("browser_enabled", True)
    monkeypatch.setattr(cv, "browser_incapability", lambda _chrome: "no display")  # pyright: ignore[reportUnknownArgumentType]
    cv.ensure_browser(ctx)
    stderr = capsys.readouterr().err
    assert "not applicable" in stderr
    assert "Install Node.js" not in stderr


def test_enabled_with_missing_npx_prints_the_repair_box(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    operator_database: Callable[[], Any],
    operator_pipeline: Callable[[], EventPipeline],
    *,
    command_runtime: CommandRuntime,
) -> None:
    """A repairable dependency gap keeps converge's existing warn-not-raise path."""
    ctx = _ctx(
        tmp_path,
        operator_database=operator_database,
        producer=operator_pipeline,
        command_runtime=command_runtime,
    )
    ctx.config.set_field("browser_enabled", True)
    monkeypatch.setattr(
        cv,
        "browser_incapability",
        lambda _chrome: "no npx (install Node.js for chrome-devtools-mcp)",  # pyright: ignore[reportUnknownArgumentType]
    )
    cv.ensure_browser(ctx)
    assert "Install Node.js" in capsys.readouterr().err


def test_steps_read_their_context_owner_and_later_overlay(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    operator_database: Callable[[], Any],
    operator_pipeline: Callable[[], EventPipeline],
    *,
    command_runtime: CommandRuntime,
) -> None:
    from services.desktop.browser import profile

    first = _ctx(
        tmp_path / "first",
        operator_database=operator_database,
        producer=operator_pipeline,
        command_runtime=command_runtime,
    )
    second_config = snapshot_process_config()
    second = replace(
        first,
        ava_home=tmp_path / "second",
        config=second_config,
        machine=machine_from_boot(second_config),
    )
    first.config.set_field("browser_enabled", False)
    second.config.set_field("browser_enabled", True)
    probed: list[bool] = []

    def capable(_chrome: str | None) -> None:
        probed.append(True)

    def seed(*, interactive: bool) -> None:
        assert isinstance(interactive, bool)

    monkeypatch.setattr(cv, "browser_incapability", capable)
    monkeypatch.setattr(profile, "ensure_browser_profile", seed)
    cv.ensure_browser(first)
    cv.ensure_browser(second)
    assert probed == [True]
    assert first.config is not second.config
    first.config.set_field("browser_enabled", True)
    cv.ensure_browser(first)
    assert probed == [True, True]


def test_context_read_failure_keeps_the_original_error(
    tmp_path: Path,
    operator_database: Callable[[], Any],
    operator_pipeline: Callable[[], EventPipeline],
    *,
    command_runtime: CommandRuntime,
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
        machine=command_runtime.machine,
    )
    with pytest.raises(RuntimeError) as caught:
        cv.ensure_browser(ctx)
    assert caught.value is error


def test_step_registered_agent_runner_only() -> None:
    step = next(s for s in cv.CONVERGE_STEPS if s.name == "browser capability")
    assert step.roles == frozenset({"agent-runner"})
    assert step.requires_unit_config is True


def test_plugin_config_images_step_agent_runner_only() -> None:
    """Plugin config images are read only by agent processes (agent-runners); the
    step must not run on a gateway."""
    step = next(s for s in cv.CONVERGE_STEPS if s.name == "plugin config images")
    assert step.roles == frozenset({"agent-runner"})
    assert step.requires_unit_config is True
