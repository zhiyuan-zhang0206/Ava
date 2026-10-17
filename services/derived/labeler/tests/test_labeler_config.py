from __future__ import annotations

import asyncio
import dataclasses
import os
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast
from unittest.mock import patch

import pytest

from base.cluster.dataplane.direct_url import ClusterDirectUrl
from base.cluster.machine import MachineIdentity
from base.config import ConfigBoot, Settings, get_field, settings
from base.config.domains.lm import LmSettings
from base.daemon.endpoints import ServiceEndpoint
from base.db import Database
from base.db.code_version_gate import ProcessDbGate
from base.db.config import DbConfig
from base.host.env.agent_slices import ModelOverrides
from base.lm.catalog import ModelCatalog
from base.lm.factory import build_chat_model as real_build_chat_model
from base.native_process import code_version
from base.native_process.loaded_commit import LoadedCommit
from services.derived.labeler import daemon


def test_the_slice_carries_the_live_value_of_every_field(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings.lm, "labeler_model", "label-model-x")
    monkeypatch.setattr(settings.services, "labeler_max_chars", 33)
    config = daemon.labeler_config()
    for field in dataclasses.fields(config):
        flat: Any = get_field(field.name)
        assert getattr(config, field.name) == flat
    assert (config.labeler_model, config.labeler_max_chars) == ("label-model-x", 33)


def _assert_run_lifecycle(
    monkeypatch: pytest.MonkeyPatch,
    model_catalog: ModelCatalog,
    error: Exception | None,
    *,
    database_gate: ProcessDbGate,
    process_config: ConfigBoot,
    tmp_path: Path,
) -> None:
    received: list[object] = []
    cleanup: list[str] = []
    startup: list[str] = []
    image = LoadedCommit(Path("/captured-labeler"), None)

    class _Health:
        pass

    async def fake_start(_name: str, _port: int, **_kwargs: Any) -> _Health:
        assert _kwargs["image"] is image
        startup.append("health")
        return _Health()

    async def fake_stop(_server: object) -> None:
        cleanup.append("health")

    class _Pool:
        def close(self) -> None:
            cleanup.append("pool")

    async def fake_dispatch(
        _pool: object,
        _db: object,
        _bus: object,
        _liveness: object,
        config: object,
        *,
        build_chat_model: Callable[..., Any],
        catalog: ModelCatalog,
        llm_override: str,
        overrides: ModelOverrides,
    ) -> None:
        assert build_chat_model is real_build_chat_model
        assert catalog is model_catalog
        assert llm_override == settings.lm.llm_override
        assert overrides == daemon.labeler_model_overrides(profile=settings.profile, lm=settings.lm)
        received.append(config)
        if error is not None:
            raise error

    def is_running_for_entry(config: ConfigBoot, *, runtime: Path) -> bool:
        return False

    monkeypatch.setattr(daemon, "_is_running", is_running_for_entry)

    def write_pidfile_for_entry(config: ConfigBoot, *, runtime: Path) -> None:
        return startup.append("pidfile")

    monkeypatch.setattr(daemon, "write_pidfile", write_pidfile_for_entry)

    def remove_pidfile_for_entry(config: ConfigBoot, *, runtime: Path) -> None:
        return cleanup.append("pidfile")

    monkeypatch.setattr(daemon, "_remove_pidfile", remove_pidfile_for_entry)
    monkeypatch.setattr(daemon, "start_health_server", fake_start)
    monkeypatch.setattr(daemon, "stop_health_server", fake_stop)
    monkeypatch.setattr(daemon, "build_model_catalog", lambda: model_catalog)

    def fake_pool(_self: object) -> _Pool:
        startup.append("pool")
        return _Pool()

    def database() -> Database:
        startup.append("database")
        return Database.from_settings(gate=database_gate)

    def endpoint_for_entry(config: ConfigBoot, *, runtime: Path) -> ServiceEndpoint:
        return ServiceEndpoint("labeler", 1, Path("/nonexistent/labeler.pid"))

    monkeypatch.setattr(daemon, "labeler_endpoint", endpoint_for_entry)
    monkeypatch.setattr(daemon.Database, "pool", fake_pool)
    monkeypatch.setattr(daemon, "dispatch_loop", fake_dispatch)

    if error is None:
        asyncio.run(
            daemon.run(
                process_config=process_config,
                runtime=tmp_path / "run",
                database=database,
                image=image,
            )
        )
    else:
        with pytest.raises(type(error)) as caught:
            asyncio.run(
                daemon.run(
                    process_config=process_config,
                    runtime=tmp_path / "run",
                    database=database,
                    image=image,
                )
            )
        assert caught.value is error
    assert received == [daemon.labeler_config()]
    assert cleanup == ["pool", "health", "pidfile"]
    assert startup == ["pidfile", "health", "database", "pool"]


def _assert_gate_matches_image(
    gates: list[ProcessDbGate], reads: list[tuple[Path, str]], image: LoadedCommit
) -> None:
    assert len(gates) == 2 and gates[0] is gates[1]
    if image.sha is None:
        with pytest.raises(code_version.CodeVersionError, match="loaded commit"):
            gates[0].application_name()
        assert reads == []
    else:
        assert gates[0].application_name() == gates[1].application_name() == "meiva:labeler:v42"
        assert reads == [(image.source_root, image.sha)]


def _run_main(monkeypatch: pytest.MonkeyPatch, *, machine_name: str) -> None:
    boot = ConfigBoot()
    boot.set_field("machine_name", machine_name)
    monkeypatch.setattr(daemon, "ConfigBoot", lambda: boot)
    runner = asyncio.Runner()
    monkeypatch.setattr(daemon.asyncio, "Runner", lambda: runner)
    try:
        with patch.dict(os.environ):
            daemon.main()
    finally:
        runner.close()
        tzset = getattr(time, "tzset", None)
        if tzset is not None:
            tzset()


def _ignore_entry_resource(config: ConfigBoot, *, runtime: Path) -> None:
    pass


@pytest.mark.parametrize("sha", ["captured-labeler", None])
def test_entry_shares_the_captured_gate_with_logging_and_work(
    monkeypatch: pytest.MonkeyPatch, sha: str | None
) -> None:
    from base.deploy.schema import migrations

    image = LoadedCommit(Path("/entry-labeler"), sha)
    stages: list[str] = []
    gates: list[ProcessDbGate] = []
    reads: list[tuple[Path, str]] = []
    factories: list[Callable[[], Database]] = []
    machines: list[MachineIdentity] = []
    pipeline = object()

    class Store:
        def __init__(
            self, config: DbConfig, *, gate: ProcessDbGate, direct_url: ClusterDirectUrl
        ) -> None:
            assert direct_url.local_host == machines[0].host
            gates.append(gate)

    def capture() -> LoadedCommit:
        stages.append("capture")
        return image

    def count(root: Path, commit: str) -> int:
        reads.append((root, commit))
        return 42

    def build(*, database: Callable[[], Database], machine: MachineIdentity, home: Path) -> object:
        machines.append(machine)
        stages.append("producer")
        factories.append(database)
        return pipeline

    def initialize(**inputs: Any) -> None:
        stages.append("logging")
        assert inputs["image"] is image
        assert inputs["producer"]() is pipeline
        assert inputs["machine_reader"] == machines[0].name
        assert inputs["machine_reader"]() == "labeler-entry"
        factories[0]()

    async def work(
        *,
        process_config: ConfigBoot,
        runtime: Path,
        database: Callable[[], Database],
        image: LoadedCommit,
    ) -> None:
        stages.append("run")
        assert image is captured
        assert database is factories[0]
        database()

    captured = image

    def checked_schema(_url: str) -> None:
        stages.append("schema")

    def no_signal_handlers(_name: str) -> None:
        pass

    def exit_process(code: int) -> None:
        stages.append(f"exit:{code}")

    monkeypatch.setattr(daemon.LoadedCommit, "capture", capture)
    monkeypatch.setattr(code_version, "first_parent_count", count)
    monkeypatch.setattr(daemon, "Database", Store)
    monkeypatch.setattr(daemon, "build_pipeline", build)
    monkeypatch.setattr(daemon, "init_gateway_process", initialize)
    monkeypatch.setattr(migrations, "assert_schema_current", checked_schema)
    monkeypatch.setattr(daemon, "install_graceful_shutdown", no_signal_handlers)
    monkeypatch.setattr(daemon, "run", work)

    monkeypatch.setattr(daemon, "_remove_pidfile", _ignore_entry_resource)
    monkeypatch.setattr(daemon, "_hard_exit", exit_process)
    _run_main(monkeypatch, machine_name="labeler-entry")
    assert stages == ["capture", "schema", "producer", "logging", "run", "exit:0"]
    _assert_gate_matches_image(gates, reads, image)


def test_run_hands_the_slice_to_the_dispatch_loop(
    monkeypatch: pytest.MonkeyPatch,
    model_catalog: ModelCatalog,
    *,
    database_gate: ProcessDbGate,
    process_config: ConfigBoot,
    tmp_path: Path,
) -> None:
    _assert_run_lifecycle(
        monkeypatch,
        model_catalog,
        None,
        database_gate=database_gate,
        process_config=process_config,
        tmp_path=tmp_path,
    )


def test_run_cleans_up_after_dispatch_failure(
    monkeypatch: pytest.MonkeyPatch,
    model_catalog: ModelCatalog,
    *,
    database_gate: ProcessDbGate,
    process_config: ConfigBoot,
    tmp_path: Path,
) -> None:
    _assert_run_lifecycle(
        monkeypatch,
        model_catalog,
        TypeError("invalid dispatch"),
        database_gate=database_gate,
        process_config=process_config,
        tmp_path=tmp_path,
    )


def test_main_reports_uncaught_failure_and_exits_nonzero(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    from base.deploy.schema import migrations

    error = TypeError("invalid labeler dispatch")
    removed: list[bool] = []

    class _Exit(BaseException):
        def __init__(self, code: int) -> None:
            self.code = code

    async def fail_run(**_kwargs: object) -> None:
        raise error

    def exit_process(code: int) -> None:
        raise _Exit(code)

    def no_schema_check(_url: str) -> None:
        pass

    def no_logger_init(**_kwargs: object) -> None:
        pass

    def no_signal_handlers(_name: str) -> None:
        pass

    monkeypatch.setattr(migrations, "assert_schema_current", no_schema_check)
    monkeypatch.setattr(daemon, "init_gateway_process", no_logger_init)
    monkeypatch.setattr(daemon, "install_graceful_shutdown", no_signal_handlers)
    monkeypatch.setattr(daemon, "run", fail_run)

    def remove_pidfile_for_entry(config: ConfigBoot, *, runtime: Path) -> None:
        return removed.append(True)

    monkeypatch.setattr(daemon, "_remove_pidfile", remove_pidfile_for_entry)
    monkeypatch.setattr(daemon, "_hard_exit", exit_process)
    runner = asyncio.Runner()
    monkeypatch.setattr(daemon.asyncio, "Runner", lambda: runner)
    try:
        with pytest.raises(_Exit) as caught, patch.dict(os.environ):
            daemon.main()
        assert caught.value.code == 1
    finally:
        runner.close()
        tzset = getattr(time, "tzset", None)
        if tzset is not None:
            tzset()
    assert removed == [True]
    [record] = [item for item in caplog.records if "daemon crashed" in item.message]
    assert record.exc_info is not None
    assert record.exc_info[1] is error


def test_gateway_tuning_does_not_read_stripped_agent_fields() -> None:
    runtime = Settings(profile="gateway")

    class _PoisonLm:
        @property
        def reasoning_effort(self) -> str:
            raise AssertionError("gateway must not read stripped reasoning effort")

        @property
        def claude_thinking_budget_tokens(self) -> int:
            raise AssertionError("gateway must not read stripped thinking budget")

    assert runtime.profile == "gateway"
    actual = daemon.labeler_model_overrides(
        profile=runtime.profile, lm=cast(LmSettings, _PoisonLm())
    )
    assert actual == ModelOverrides.from_pins({})


@pytest.mark.parametrize("effort,budget", [(None, None), ("high", 2048), ("", 0)])
def test_full_tuning_keeps_explicit_values(effort: str | None, budget: int | None) -> None:
    lm = LmSettings.model_validate(
        {"reasoning_effort": effort, "claude_thinking_budget_tokens": budget}
    )
    runtime = Settings(profile=None, lm=lm)
    actual = daemon.labeler_model_overrides(profile=None, lm=runtime.lm)
    assert actual.reasoning_effort == effort
    assert actual.claude_thinking_budget_tokens == budget


def test_labeler_tuning_refuses_unknown_profile() -> None:
    with pytest.raises(ValueError, match="not a known process profile"):
        daemon.labeler_model_overrides(profile="unknown", lm=LmSettings())
