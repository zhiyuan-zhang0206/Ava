"""The schedule process composes SDK inputs at the execution boundaries."""

import subprocess
import sys
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

import psycopg
import pytest

from base.agents.context import AvaContext
from base.agents.context.clients import ClientSet
from base.clock import Clock
from base.config import ConfigBoot
from base.daemon.schedules.inputs import ScheduleInputs
from base.db import Database
from base.db.code_version_gate import ProcessDbGate
from base.db.config import DbConfig
from base.native_process.loaded_commit import LoadedCommit
from base.telemetry import EventPipeline
from schedules.entry import schedule_entry as runner_inputs_entry
from services.wake.schedule_manager import runner


def _schedule(conn: psycopg.Connection) -> int:
    row = conn.execute(
        "INSERT INTO schedules (name, script, command) "
        "VALUES ('composition', 'pass', 'python schedule.py') RETURNING id"
    ).fetchone()
    conn.commit()
    assert row is not None
    return row[0]


def test_actor_binding_failure_does_not_open_history(
    db_conn: psycopg.Connection, unit_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sid = _schedule(db_conn)
    error = RuntimeError("actor unavailable")

    def bind(context: AvaContext) -> None:
        assert context.identity is not None
        assert context.identity.actor == f"schedule:{sid}"
        assert (unit_home / "schedules" / str(sid) / "schedule.py").read_text() == "pass"
        raise error

    monkeypatch.setattr(runner.ava, "bind_context", bind)
    with pytest.raises(RuntimeError) as caught:
        runner.run(sid)
    assert caught.value is error
    assert db_conn.execute("SELECT count(*) FROM schedule_runs").fetchone() == (0,)


def test_plugins_load_under_guard_after_actor_and_history(
    db_conn: psycopg.Connection, unit_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sid = _schedule(db_conn)
    guards: list[threading.Thread] = []

    def plugins(
        *, config: ConfigBoot, clock_factory: Callable[[], Clock], producer: Callable[[], Any]
    ) -> None:
        from ava.sdk_surface.agent_identity import require_actor

        assert require_actor() == f"schedule:{sid}"
        assert runner.ava.context.require_clock().timezone == clock_factory().timezone
        assert producer == runner.ava.context.clients.event_pipeline
        assert config.view.general.timezone == clock_factory().timezone
        assert (unit_home / "schedules" / str(sid) / "schedule.py").exists()
        assert db_conn.execute("SELECT ok FROM schedule_runs").fetchall() == [(None,)]
        guards.extend(t for t in threading.enumerate() if t.name == f"schedule-{sid}-stall-guard")
        assert len(guards) == 1 and guards[0].is_alive()
        raise RuntimeError("plugin failure")

    monkeypatch.setattr(runner.ava, "ensure_plugins_loaded", plugins)
    assert runner.run(sid) == 1
    assert not guards[0].is_alive()
    assert db_conn.execute("SELECT ok FROM schedule_runs").fetchall() == [(False,)]


def test_retired_entrypoint_fails_without_execution(unit_home: Path) -> None:
    result = subprocess.run(
        [sys.executable, "-m", "gateway.schedules.runner", "1"],
        cwd=Path(__file__).resolve().parents[4],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert result.returncode == 1
    assert "services.wake.schedule_manager.runner" in result.stderr
    assert not (unit_home / "schedules").exists()


def test_main_refuses_on_foreign_checkout(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(_repo: Path) -> str:
        return "foreign checkout"

    monkeypatch.setattr(sys, "argv", ["schedule_runner", "1"])
    monkeypatch.setattr(runner, "prod_service_checkout_error", refuse)
    with pytest.raises(SystemExit) as caught:
        runner.main()
    assert caught.value.code == 3


@pytest.mark.parametrize("sha", ["a" * 40, None])
def test_process_owns_one_gate_and_closes_its_pipeline(
    monkeypatch: pytest.MonkeyPatch, sha: str | None
) -> None:
    image = LoadedCommit(Path("/loaded-schedule"), sha)
    images: list[LoadedCommit] = []
    gates: list[ProcessDbGate] = []
    pipelines: list[EventPipeline] = []
    version_type = runner.CodeVersion

    def version(loaded: LoadedCommit) -> Any:
        images.append(loaded)
        return version_type(loaded)

    def database(
        config: DbConfig, *, gate: ProcessDbGate, local_host: Callable[[], str]
    ) -> Database:
        gates.append(gate)
        return Database(config, gate=gate, local_host=local_host)

    def execute(
        schedule_id: int,
        revision: int | None,
        *,
        database: Callable[[], Database],
        bind_actor: Callable[[int], None],
        load_plugins: Callable[[], None],
        inputs: ScheduleInputs,
    ) -> int:
        assert (schedule_id, revision) == (17, 3)
        database()
        bind_actor(schedule_id)
        context = runner.ava.context
        assert inputs.database is database
        assert inputs.producer == context.clients.event_pipeline
        assert inputs.image is image
        with runner_inputs_entry(inputs) as borrowed:
            assert borrowed is inputs
            borrowed.database()
        context.clients.database()
        pipelines.append(context.clients.event_pipeline())
        assert not pipelines[0].stopped
        # A later source fact must not replace the entry's captured image.
        monkeypatch.setattr(
            runner.ava, "loaded_code_image", lambda: LoadedCommit(Path("/late"), "b")
        )
        database()
        return 0

    monkeypatch.setattr(runner.ava, "loaded_code_image", lambda: image)
    monkeypatch.setattr(runner, "CodeVersion", version)
    monkeypatch.setattr(runner, "Database", database)
    monkeypatch.setattr(runner.engine, "run", execute)
    assert runner.run(17, 3) == 0
    assert images == [image]
    assert len(gates) == 4 and all(gate is gates[0] for gate in gates)
    assert pipelines[0].stopped


def test_actor_failure_stays_primary_when_client_cleanup_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    primary = RuntimeError("actor unavailable")
    secondary = OSError("client close failed")
    close = ClientSet.close

    def bind(_context: AvaContext) -> None:
        raise primary

    def execute(
        schedule_id: int,
        _revision: int | None,
        *,
        database: Callable[[], Database],
        bind_actor: Callable[[int], None],
        load_plugins: Callable[[], None],
        inputs: ScheduleInputs,
    ) -> int:
        bind_actor(schedule_id)
        raise AssertionError("binding failure was swallowed")

    def fail_close(self: ClientSet, *, pipeline_timeout: float = 5) -> None:
        assert pipeline_timeout == 2
        close(self, pipeline_timeout=pipeline_timeout)
        raise secondary

    monkeypatch.setattr(runner.ava, "bind_context", bind)
    monkeypatch.setattr(runner.engine, "run", execute)
    monkeypatch.setattr(ClientSet, "close", fail_close)
    with pytest.raises(RuntimeError) as caught:
        runner.run(17)
    assert caught.value is primary
    assert any("client close failed" in note for note in primary.__notes__)


def test_runpy_script_receives_the_same_entry_builders(
    db_conn: psycopg.Connection, unit_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sid = _schedule(db_conn)
    marker = unit_home / "borrowed.txt"
    script = (
        "import ava\n"
        "from pathlib import Path\n"
        "from schedules.entry import schedule_entry\n"
        "with schedule_entry(AVA_SCHEDULE_INPUTS) as inputs:\n"
        "    assert inputs is AVA_SCHEDULE_INPUTS\n"
        "    assert inputs.producer == ava.context.clients.event_pipeline\n"
        "    assert inputs.image is ava.loaded_code_image()\n"
        f"    Path({str(marker)!r}).write_text('borrowed')\n"
    )
    db_conn.execute("UPDATE schedules SET script = %s WHERE id = %s", (script, sid))
    db_conn.commit()

    def plugins(
        *, config: ConfigBoot, clock_factory: Callable[[], Clock], producer: Callable[[], Any]
    ) -> None:
        pass

    monkeypatch.setattr(runner.ava, "ensure_plugins_loaded", plugins)
    assert runner.run(sid) == 0
    assert marker.read_text() == "borrowed"
