from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import yaml

from base import config
from base.cluster.machine import MachineIdentity
from base.host.system.hostport import Host
from base.telemetry import EventPipeline
from base.telemetry.lgtm_local import service_argv
from cli.commands.observability import lgtm_native
from cli.commands.observability.tests.lgtm_inputs import (
    darwin_host as darwin_host,
)
from cli.commands.observability.tests.lgtm_inputs import (
    default_provisioning as default_provisioning,
)
from cli.commands.observability.tests.lgtm_inputs import (
    empty_native_versions as _empty_native_versions,
)
from cli.database.tests.command_inputs import command_machine as command_machine
from tests.path_scoped.cli_tests import operator_database as operator_database
from tests.path_scoped.cli_tests import operator_pipeline as operator_pipeline


def test_station_role_renders_full_native_set_without_marker(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    operator_database: Callable[[], Any],
    operator_pipeline: Callable[[], EventPipeline],
    darwin_host: Host,
    *,
    command_machine: MachineIdentity,
) -> None:
    repo = Path(__file__).resolve().parents[4]
    home = tmp_path / "station-home"
    agents_dir = tmp_path / "LaunchAgents"
    agents_dir.mkdir()
    monkeypatch.setattr(lgtm_native, "native_versions", _empty_native_versions)

    lgtm_native.ensure_lgtm_native(
        repo,
        home,
        services=frozenset(lgtm_native.BACKENDS),
        database_factory=operator_database,
        producer=operator_pipeline,
        machine=command_machine,
        host=darwin_host,
    )

    native_dir = home / "lgtm/native"
    for name in ("loki.yaml", "prometheus.yml", "grafana.ini", "runtime.env"):
        assert (native_dir / "config" / name).is_file()
    assert (native_dir / "grafana" / "run.sh").is_file()
    assert not list(agents_dir.iterdir())
    assert (native_dir / "data" / "loki").is_dir()
    assert (native_dir / "data" / "prom").is_dir()
    rendered_loki = (native_dir / "config" / "loki.yaml").read_text(encoding="utf-8")
    assert "{{AVA_HOME}}" not in rendered_loki
    assert "{{LGTM_STORAGE_DIR}}" not in rendered_loki


def test_native_storage_dir_default_matches_historical_layout(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    operator_database: Callable[[], Any],
    operator_pipeline: Callable[[], EventPipeline],
    *,
    command_machine: MachineIdentity,
) -> None:
    repo = Path(__file__).resolve().parents[4]
    home = tmp_path / "home"
    native_dir = home / "lgtm/native"
    monkeypatch.setattr(config.settings.observability, "lgtm_storage_dir", "")
    lgtm_native.render_configs(
        repo,
        native_dir,
        home,
        database_factory=operator_database,
        producer=operator_pipeline,
        machine=command_machine,
    )
    rendered_loki = yaml.safe_load((native_dir / "config/loki.yaml").read_text(encoding="utf-8"))
    assert rendered_loki["common"]["path_prefix"] == str((home / "lgtm/native/data/loki").resolve())
    assert rendered_loki["compactor"]["working_directory"] == str(
        (home / "lgtm/native/data/loki/compactor").resolve()
    )
    prometheus_argv = service_argv(home, "prometheus")
    assert "--storage.tsdb.path=" + str((home / "lgtm/native/data/prom").resolve()) in (
        prometheus_argv
    )


def test_native_storage_dir_parameterized(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    operator_database: Callable[[], Any],
    operator_pipeline: Callable[[], EventPipeline],
    *,
    command_machine: MachineIdentity,
) -> None:
    repo = Path(__file__).resolve().parents[4]
    home = tmp_path / "home"
    native_dir = home / "lgtm/native"
    monkeypatch.setattr(config.settings.observability, "lgtm_storage_dir", "/data/obs")
    lgtm_native.render_configs(
        repo,
        native_dir,
        home,
        database_factory=operator_database,
        producer=operator_pipeline,
        machine=command_machine,
    )
    rendered_loki = yaml.safe_load((native_dir / "config/loki.yaml").read_text(encoding="utf-8"))
    assert rendered_loki["common"]["path_prefix"] == "/data/obs/loki"
    assert (
        rendered_loki["common"]["storage"]["filesystem"]["chunks_directory"]
        == "/data/obs/loki/chunks"
    )
    assert rendered_loki["compactor"]["working_directory"] == "/data/obs/loki/compactor"
    prometheus_argv = service_argv(home, "prometheus")
    assert "--storage.tsdb.path=/data/obs/prom" in prometheus_argv


def test_station_role_creates_configured_storage_dirs(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    operator_database: Callable[[], Any],
    operator_pipeline: Callable[[], EventPipeline],
    darwin_host: Host,
    *,
    command_machine: MachineIdentity,
) -> None:
    repo = Path(__file__).resolve().parents[4]
    home = tmp_path / "station-home"
    agents_dir = tmp_path / "LaunchAgents"
    agents_dir.mkdir()
    storage = tmp_path / "obs-data"
    monkeypatch.setattr(lgtm_native, "native_versions", _empty_native_versions)
    monkeypatch.setattr(config.settings.observability, "lgtm_storage_dir", str(storage))

    lgtm_native.ensure_lgtm_native(
        repo,
        home,
        services=frozenset(lgtm_native.BACKENDS),
        database_factory=operator_database,
        producer=operator_pipeline,
        machine=command_machine,
        host=darwin_host,
    )

    assert (storage / "loki").is_dir()
    assert (storage / "prom").is_dir()
    assert (home / "lgtm/native/data").exists() is False
