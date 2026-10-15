from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

import cli.commands.converge.brew_pin as cbp
import cli.commands.lifecycle.converge_host as cv
from base.clock.tests.manual_timers import ManualTimers
from base.cluster.machine.boot import machine_from_boot
from base.config import ConfigBoot
from base.host import brew_pin
from base.host.system.hostport import Host
from base.host.system.tests.fake_commands import (
    DARWIN_ARM64,
    LINUX_X86,
    FakeCommands,
    Reply,
    fake_host,
)
from base.sessions.tests.refuse_helper import refuse_helper
from base.telemetry import EventPipeline
from cli.database.tests.command_inputs import command_runtime as command_runtime
from tests.path_scoped.cli_tests import operator_database as operator_database
from tests.path_scoped.cli_tests import operator_pipeline as operator_pipeline

EXPECTED_PINNED_FORMULAE = frozenset(
    {
        "ca-certificates",
        "cloudflared",
        "grafana",
        "json-c",
        "node",
        "openssl@3",
        "pgbouncer",
        "pgvector",
        "postgresql@17",
        "redis",
        "redis@8.2",
        "uv",
    }
)


def _ctx(
    tmp_path: Path,
    host: Host,
    *,
    operator_database: Callable[[], Any],
    producer: Callable[[], EventPipeline],
) -> cv.ConvergeCtx:
    machine_config = ConfigBoot()
    return cv.ConvergeCtx(
        repo=Path("/repo"),
        ava_home=tmp_path,
        roles=cv.ALL_ROLES,
        config=machine_config,
        database_factory=operator_database,
        producer=producer,
        machine=machine_from_boot(machine_config),
        helper=refuse_helper,
        host=host,
        services=frozenset(),
        timers=ManualTimers(),
    )


def _brew(pinned: set[str], installed: set[str] | None = None) -> Host:
    installed_formulae = set(EXPECTED_PINNED_FORMULAE) if installed is None else installed
    commands = FakeCommands()
    commands.on(["brew", "list", "--pinned"], Reply("\n".join(sorted(pinned))))
    commands.on(["brew", "list", "--formula"], Reply("\n".join(sorted(installed_formulae))))
    return fake_host(commands, platform=DARWIN_ARM64)


def test_pinned_formula_manifest_is_exact() -> None:
    assert brew_pin.PINNED_BREW_FORMULAE == EXPECTED_PINNED_FORMULAE


def test_step_is_registered_after_firewall_for_both_roles() -> None:
    steps = cv.CONVERGE_STEPS
    brew_pin_index = next(i for i, step in enumerate(steps) if step.apply is cbp.ensure_brew_pin)
    firewall_index = next(
        i for i, step in enumerate(steps) if step.apply.__name__ == "ensure_firewall_allowlist"
    )
    step = steps[brew_pin_index]
    assert brew_pin_index > firewall_index
    assert step.roles == cv.ALL_ROLES
    assert step.requires_unit_config is False


def test_all_formulae_pinned_is_silent(
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    operator_database: Callable[[], Any],
    operator_pipeline: Callable[[], EventPipeline],
) -> None:
    host = _brew(set(EXPECTED_PINNED_FORMULAE))

    cbp.ensure_brew_pin(
        _ctx(tmp_path, host, operator_database=operator_database, producer=operator_pipeline)
    )

    assert capsys.readouterr().err == ""


def test_missing_formula_warns_with_manual_repin_command(
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    operator_database: Callable[[], Any],
    operator_pipeline: Callable[[], EventPipeline],
) -> None:
    host = _brew(set(EXPECTED_PINNED_FORMULAE - {"redis@8.2"}))

    cbp.ensure_brew_pin(
        _ctx(tmp_path, host, operator_database=operator_database, producer=operator_pipeline)
    )

    warning = capsys.readouterr().err
    assert warning.startswith("  ! brew-pin:")
    assert "redis@8.2" in warning
    assert "brew pin redis@8.2" in warning


def test_uninstalled_manifest_formula_is_not_flagged(
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    operator_database: Callable[[], Any],
    operator_pipeline: Callable[[], EventPipeline],
) -> None:
    installed = set(EXPECTED_PINNED_FORMULAE - {"grafana"})
    host = _brew(installed, installed=installed)

    cbp.ensure_brew_pin(
        _ctx(tmp_path, host, operator_database=operator_database, producer=operator_pipeline)
    )

    assert capsys.readouterr().err == ""


def test_installed_but_unpinned_formula_still_warns(
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    operator_database: Callable[[], Any],
    operator_pipeline: Callable[[], EventPipeline],
) -> None:
    pinned = set(EXPECTED_PINNED_FORMULAE - {"grafana"})
    host = _brew(pinned, installed=set(EXPECTED_PINNED_FORMULAE))

    cbp.ensure_brew_pin(
        _ctx(tmp_path, host, operator_database=operator_database, producer=operator_pipeline)
    )

    warning = capsys.readouterr().err
    assert "grafana" in warning
    assert "brew pin grafana" in warning


def test_brew_absent_is_silent(
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    operator_database: Callable[[], Any],
    operator_pipeline: Callable[[], EventPipeline],
) -> None:

    commands = FakeCommands()
    commands.on("brew", FileNotFoundError("brew"), repeat=True)
    host = fake_host(commands, platform=DARWIN_ARM64)

    cbp.ensure_brew_pin(
        _ctx(tmp_path, host, operator_database=operator_database, producer=operator_pipeline)
    )

    assert capsys.readouterr().err == ""


def test_non_macos_is_silent_without_calling_brew(
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    operator_database: Callable[[], Any],
    operator_pipeline: Callable[[], EventPipeline],
) -> None:
    commands = FakeCommands()
    host = fake_host(commands, platform=LINUX_X86)

    cbp.ensure_brew_pin(
        _ctx(tmp_path, host, operator_database=operator_database, producer=operator_pipeline)
    )

    assert capsys.readouterr().err == ""
    assert commands.argvs == []
