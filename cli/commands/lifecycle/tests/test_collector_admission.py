"""Collector preparation retains explicit gateway opt-in and never reconciles a live tree."""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from base.cluster.machine import MachineIdentity, MachineRoles
from base.cluster.machine.boot import machine_from_boot
from base.config import ConfigBoot
from base.telemetry import EventPipeline
from cli.commands.lifecycle import root_driver
from cli.commands.observability import otel_collector as oc
from cli.database.tests.command_inputs import command_runtime as command_runtime
from tests.path_scoped.cli_tests import operator_database as operator_database
from tests.path_scoped.cli_tests import operator_pipeline as operator_pipeline


def _fail_ensure_otel_collector(*_args: object, **_kwargs: object) -> None:
    pytest.fail("must skip")


def test_non_lgtm_gateway_converge_skips_collector_install(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    operator_database: Callable[[], Any],
    operator_pipeline: Callable[[], EventPipeline],
) -> None:
    machine_config_763 = ConfigBoot()
    machine_config_763.set_field("machine_serve_gateway", True)
    machine_config_763.set_field("machine_serve_agent_runner", False)
    machine_config_763.set_field("machine_serve_observability_station", False)
    home = tmp_path / ".ava-preview"
    home.mkdir()
    monkeypatch.delitem(os.environ, "AVA_TELEMETRY_OTLP_ENDPOINT", raising=False)
    ctx = oc.ConvergeCtx(
        repo=Path(__file__).resolve().parents[4],
        ava_home=home,
        roles=frozenset({"gateway", "agent-runner"}),
        config=machine_config_763,
        database_factory=operator_database,
        producer=operator_pipeline,
        machine=machine_from_boot(machine_config_763),
    )

    def must_not_install(*_args: object, **_kwargs: object) -> None:
        pytest.fail("a non-LGTM gateway must not install a collector")

    monkeypatch.setattr(oc, "ensure_otel_collector", must_not_install)

    oc.ensure_otel_collector_step(ctx)

    assert not (home / "otel-collector").exists()
    err = capsys.readouterr().err
    assert "gateway" in err
    assert "lgtm-host" in err
    assert "collector skipped" in err


def test_non_lgtm_gateway_with_explicit_endpoint_installs_collector(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    operator_database: Callable[[], Any],
    operator_pipeline: Callable[[], EventPipeline],
) -> None:
    """An explicit AVA_TELEMETRY_OTLP_ENDPOINT override opens the converge
    step on a non-LGTM gateway: the operator opted into explicit export, so
    the local sidecar is installed instead of skipped/reaped."""
    machine_config_798 = ConfigBoot()
    machine_config_798.set_field("machine_serve_gateway", True)
    machine_config_798.set_field("machine_serve_agent_runner", False)
    machine_config_798.set_field("machine_serve_observability_station", False)
    home = tmp_path / ".ava-preview"
    home.mkdir()
    monkeypatch.setitem(os.environ, "AVA_TELEMETRY_OTLP_ENDPOINT", "http://collector.invalid:4318")
    ctx = oc.ConvergeCtx(
        repo=Path(__file__).resolve().parents[4],
        ava_home=home,
        roles=frozenset({"gateway"}),
        config=machine_config_798,
        database_factory=operator_database,
        producer=operator_pipeline,
        machine=machine_from_boot(machine_config_798),
    )

    installed: list[tuple[object, object, object]] = []

    def install(
        repo: Path,
        home: Path,
        roles: MachineRoles | None,
        *,
        database_factory: Callable[[], Any],
        machine: MachineIdentity,
    ) -> None:
        assert machine is ctx.machine
        assert database_factory is operator_database
        installed.append((repo, home, roles))

    monkeypatch.setattr(oc, "ensure_otel_collector", install)

    oc.ensure_otel_collector_step(ctx)

    assert len(installed) == 1
    assert installed[0][1] == home


def test_collector_preparation_never_controls_a_running_service_tree(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    operator_database: Callable[[], Any],
    operator_pipeline: Callable[[], EventPipeline],
) -> None:
    machine_config_829 = ConfigBoot()
    machine_config_829.set_field("machine_serve_gateway", True)
    machine_config_829.set_field("machine_serve_agent_runner", False)
    machine_config_829.set_field("machine_serve_observability_station", False)
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.delitem(os.environ, "AVA_TELEMETRY_OTLP_ENDPOINT", raising=False)
    ctx = oc.ConvergeCtx(
        repo=Path(__file__).resolve().parents[4],
        ava_home=home,
        roles=frozenset({"gateway"}),
        config=machine_config_829,
        database_factory=operator_database,
        producer=operator_pipeline,
        machine=machine_from_boot(machine_config_829),
    )

    def no_root_control() -> None:
        pytest.fail("Preparation cannot independently reconcile a live root")

    monkeypatch.setattr(oc, "ensure_otel_collector", _fail_ensure_otel_collector)
    monkeypatch.setattr(root_driver, "root_client", no_root_control)
    oc.ensure_otel_collector_step(ctx)


def test_non_lgtm_gateway_reports_and_preserves_residual_config(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    operator_database: Callable[[], Any],
    operator_pipeline: Callable[[], EventPipeline],
) -> None:
    machine_config_859 = ConfigBoot()
    machine_config_859.set_field("machine_serve_gateway", True)
    machine_config_859.set_field("machine_serve_agent_runner", False)
    machine_config_859.set_field("machine_serve_observability_station", False)
    home = tmp_path / ".ava-preview"
    collector = home / "otel-collector"
    collector.mkdir(parents=True)
    config = collector / "config.yaml"
    config.write_text("stale: true\n", encoding="utf-8")
    monkeypatch.delitem(os.environ, "AVA_TELEMETRY_OTLP_ENDPOINT", raising=False)
    ctx = oc.ConvergeCtx(
        repo=Path(__file__).resolve().parents[4],
        ava_home=home,
        roles=frozenset({"gateway"}),
        config=machine_config_859,
        database_factory=operator_database,
        producer=operator_pipeline,
        machine=machine_from_boot(machine_config_859),
    )

    monkeypatch.setattr(oc, "ensure_otel_collector", _fail_ensure_otel_collector)
    oc.ensure_otel_collector_step(ctx)

    assert config.read_text(encoding="utf-8") == "stale: true\n"
    assert "stale/residual" in capsys.readouterr().err
