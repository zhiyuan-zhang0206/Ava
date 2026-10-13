"""The host's bare `ava`: a plain link to the production checkout's CLI.

A production home's converge points `~/.local/bin/ava` at `<checkout>/.venv/bin/ava`
and writes no per-home CLI link; the home a run acts on is `AVA_HOME`, else
`~/.ava`, like every CLI. Until a home's first start has run, the hints name the
checkout's own CLI."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from base.telemetry import EventPipeline
from cli.commands.lifecycle import converge_host
from cli.database.runtime import CommandRuntime
from cli.tests.bootstrap.command_inputs import command_runtime as command_runtime
from tests.path_scoped.cli_tests import operator_database as operator_database
from tests.path_scoped.cli_tests import operator_pipeline as operator_pipeline

# The converge step through which a first start (`_prepare_cold_start`) wires the
# CLI, taken from CONVERGE_STEPS with its real host-global gating.
_CLI_LINK_STEPS = tuple(step for step in converge_host.CONVERGE_STEPS if step.name == "ava on PATH")


def _first_start_converge(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    prod: bool,
    existing_target: Path | None = None,
    operator_database: Callable[[], Any],
    producer: Callable[[], EventPipeline],
    command_runtime: CommandRuntime,
) -> tuple[Path, Path, Path]:
    """Run a scratch home's first-start CLI wiring; returns (checkout, home, bare link)."""
    user_home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(user_home))
    ava_home = user_home / ".ava" if prod else user_home / ".ava-preview"
    checkout = ava_home / "source"
    checkout.mkdir(parents=True)
    bare_link = user_home / ".local" / "bin" / "ava"
    if existing_target is not None:
        bare_link.parent.mkdir(parents=True)
        bare_link.symlink_to(existing_target)
    converge_host.converge_host(
        checkout,
        frozenset({"gateway", "agent-runner"}),
        ava_home=ava_home,
        steps=_CLI_LINK_STEPS,
        services=frozenset(),
        database_factory=operator_database,
        producer=producer,
        command=command_runtime,
    )
    return checkout, ava_home, bare_link


def test_prod_first_start_links_bare_ava_to_the_checkout_cli(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    operator_database: Callable[[], Any],
    operator_pipeline: Callable[[], EventPipeline],
    *,
    command_runtime: CommandRuntime,
) -> None:
    checkout, ava_home, bare_link = _first_start_converge(
        tmp_path,
        monkeypatch,
        prod=True,
        operator_database=operator_database,
        producer=operator_pipeline,
        command_runtime=command_runtime,
    )

    assert bare_link.readlink() == checkout / ".venv" / "bin" / "ava"
    assert not (ava_home / "ava").exists() and not (ava_home / "ava").is_symlink()


def test_prod_start_repoints_the_retired_launcher_link(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    operator_database: Callable[[], Any],
    operator_pipeline: Callable[[], EventPipeline],
    *,
    command_runtime: CommandRuntime,
) -> None:
    """A host wired by the launcher is rewritten by its next start, with no manual step."""
    retired = tmp_path / "home" / ".ava" / "source" / "scripts" / "ava-launcher.sh"
    checkout, _, bare_link = _first_start_converge(
        tmp_path,
        monkeypatch,
        prod=True,
        existing_target=retired,
        operator_database=operator_database,
        producer=operator_pipeline,
        command_runtime=command_runtime,
    )

    assert bare_link.readlink() == checkout / ".venv" / "bin" / "ava"


def test_non_prod_first_start_leaves_the_host_link_alone(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    operator_database: Callable[[], Any],
    operator_pipeline: Callable[[], EventPipeline],
    *,
    command_runtime: CommandRuntime,
) -> None:
    prod_cli = tmp_path / "prod" / "source" / ".venv" / "bin" / "ava"
    _, ava_home, bare_link = _first_start_converge(
        tmp_path,
        monkeypatch,
        prod=False,
        existing_target=prod_cli,
        operator_database=operator_database,
        producer=operator_pipeline,
        command_runtime=command_runtime,
    )

    assert bare_link.readlink() == prod_cli
    assert not (ava_home / "ava").exists() and not (ava_home / "ava").is_symlink()


def test_non_prod_first_start_creates_no_bare_link_when_none_existed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    operator_database: Callable[[], Any],
    operator_pipeline: Callable[[], EventPipeline],
    *,
    command_runtime: CommandRuntime,
) -> None:
    _, _, bare_link = _first_start_converge(
        tmp_path,
        monkeypatch,
        prod=False,
        operator_database=operator_database,
        producer=operator_pipeline,
        command_runtime=command_runtime,
    )

    assert not bare_link.exists()
    assert not bare_link.is_symlink()
