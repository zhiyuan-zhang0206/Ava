"""Shared fixtures for the CLI tests (registered by `tests/fixtures/path_scopes.py`).

Orchestration tests record the local hold/resume seam while updating the
actual host posture. Without that posture effect, a fake recovery would leave
later gateway requests behind a stale 503 gate. Production service and OS job
boundaries remain guarded by the root fixtures.
"""

from __future__ import annotations

import pathlib
from collections.abc import Callable, Generator
from contextlib import AbstractContextManager, contextmanager

import pytest

from base.cluster.machine import MachineIdentity
from base.cluster.machine.boot import machine_from_boot
from base.config import ConfigBoot
from base.deploy.lifecycle import service_selection as ds
from base.paths import meiva_home
from base.sessions.pty.tests.pty_reaper import pty_reaper as pty_reaper
from base.telemetry import EventPipeline, process_logging
from cli.commands.tests.health_port_guard import guard_health_port_gate as guard_health_port_gate
from cli.database import (
    OperatorDatabaseFactory,
    OperatorEventPipeline,
    open_operator_database,
    operator_database_factory,
    operator_event_pipeline,
)
from cli.database.runtime import CommandRuntime
from cli.database.tests.command_inputs import command_runtime as command_runtime


@pytest.fixture(autouse=True)
def _cli_orchestration_guards(
    guard_health_port_gate: None,
    guard_service_readiness: None,
    homes_get_their_own_ports: None,
    permissions_helper_native_io: None,
) -> None:
    """Protect the same CLI effects as the package-local CLI test owner."""


@pytest.fixture
def operator_database(command_runtime: CommandRuntime) -> OperatorDatabaseFactory:
    """Borrow one real operator factory for this case's explicit CLI inputs."""
    return operator_database_factory(
        local_host=command_runtime.machine.host,
        open_database=open_operator_database,
        gateway_url_reader=lambda: command_runtime.config.view.gateway.gateway_url,
    )


@pytest.fixture
def operator_pipeline(
    operator_database: OperatorDatabaseFactory,
    command_runtime: CommandRuntime,
) -> Generator[OperatorEventPipeline]:
    """Borrow one lazy writer for the same command's work and reporter inputs."""
    producer = operator_event_pipeline(
        operator_database, machine=command_runtime.machine, home=meiva_home()
    )
    try:
        yield producer
    finally:
        producer.close()


@pytest.fixture
def _installed_machine_identity(command_runtime: CommandRuntime) -> MachineIdentity:
    """Give this case's explicit owner the provenance of an installed unit."""
    command_runtime.machine.set(name="unit-test-machine")
    return command_runtime.machine


@pytest.fixture
def as_machine(
    monkeypatch: pytest.MonkeyPatch,
) -> Callable[[pathlib.Path], AbstractContextManager[MachineIdentity]]:
    """Borrow a fresh machine owner for each real home in a cross-home test."""

    @contextmanager
    def _enter(home: pathlib.Path) -> Generator[MachineIdentity]:
        home.mkdir(parents=True, exist_ok=True)
        with monkeypatch.context() as m:
            m.setenv("MEIVA_HOME", str(home))
            config = ConfigBoot()
            config.read_process_environment()
            machine = machine_from_boot(config)
            machine.set(name=home.name)
            yield machine

    return _enter


@pytest.fixture(autouse=True)
def _isolate_disabled_services_marker(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    """Every CLI test's durable `--disable-service` marker lives in a per-test tmp
    file, never in the worker's shared session home.

    An operator `meiva start --disable-service X` persists X to
    `$MEIVA_HOME/disabled_services` (`persist_services=True` — the real, intended
    behavior the start tests exercise). The suite's session home is shared by
    every test in a worker, so an un-isolated marker outlives the test that wrote
    it: a `cmd_start(disabled_services=("restarter",))` left "restarter durably
    disabled" behind, and a later `unpause_local_cluster` test in the same worker
    early-returned — neither respawning the restarter nor raising (CI #1172/#1173
    shard-5 flake, task #2177). Same redirection `tests/components/base/test_disabled_services.py`
    uses: the marker is per-unit durable state, so each test gets a fresh one.
    """
    monkeypatch.setattr(ds, "selection_path", lambda: tmp_path / "service-selection.json")


@pytest.fixture(autouse=True)
def _isolate_local_pause_journal(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A failed stop intentionally retains its hold; it must not hold the next test."""
    from base.deploy.maintenance import pause_owner

    monkeypatch.setattr(pause_owner, "state_path", lambda: tmp_path / "pause-owner.json")
    monkeypatch.setattr(pause_owner, "lock_path", lambda: tmp_path / "pause-owner.lock")


@pytest.fixture(autouse=True)
def cli_log_sinks(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """In-process dispatch opens no real loguru sinks; the names asked for are recorded.

    `meiva start`, `restart` and `lgtm on|off` open the
    service sink set (`cli.startup_support.init_cli_logging`). A real one here would
    outlive its test: a stderr sink bound to that test's captured stream, a
    file sink in the session home and the process-wide init guard latched.
    """
    opened: list[str] = []

    def record(
        *, name: str, producer: Callable[[], EventPipeline], machine_reader: Callable[[], str]
    ) -> None:
        opened.append(name)

    monkeypatch.setattr(process_logging, "init_cli_process", record)
    return opened
