"""Child process for the schedule-runner cleanup tests: a real runner with a stalled or finished script.

Usage: `child_schedule_runner.py run <root> <schedule id> <mode>` runs the production runner entry;
`child_schedule_runner.py stall_exit <root> <blocked write>` and `... completion_during_stall <root>`
drive the stall action directly. Imports live inside the scenarios because some modes alter the
interpreter before the runner module loads.
"""

import contextlib
import sys
import threading
import time
from collections.abc import Callable
from functools import partial
from pathlib import Path
from typing import Any


def _schedule_inputs() -> tuple[Callable[[], Any], Any]:
    from base.agents.context.clients import ClientSet
    from base.cluster.machine.boot import machine_from_boot
    from base.config import ConfigBoot
    from base.daemon.schedules.inputs import ScheduleInputs
    from base.db import Database
    from base.db.code_version_gate import ProcessDbGate
    from base.native_process.code_version import CodeVersion
    from base.native_process.loaded_commit import LoadedCommit
    from base.paths import ava_home
    from base.telemetry.pipeline_build import build_pipeline

    config = ConfigBoot()
    config.read_process_environment()
    machine = machine_from_boot(config)
    image = LoadedCommit.capture()
    gate = ProcessDbGate(process="schedule-cleanup-test", version=CodeVersion(image).get)
    database = partial(Database.from_settings, gate=gate)
    clients = ClientSet(
        database=database,
        pipeline_factory=partial(
            build_pipeline, database=database, machine=machine, home=ava_home()
        ),
    )
    inputs = ScheduleInputs(database, clients.event_pipeline, image, machine=machine)
    return database, inputs


def _delay_record_error(root: Path) -> None:
    import os

    import psutil

    from services.wake.schedule_manager import engine as r

    record_error = r._record_error

    def delayed_record(*args: Any) -> None:
        (root / "record-started").touch()
        deadline = time.monotonic() + 5
        with contextlib.suppress(psutil.NoSuchProcess):
            grandchild = psutil.Process(int((root / "grandchild.pid").read_text()))
            while os.getpid() in [p.pid for p in grandchild.parents()]:
                assert time.monotonic() < deadline
                time.sleep(0.01)
        record_error(*args)

    r._record_error = delayed_record


def _delay_completion() -> None:
    from services.wake.schedule_manager import engine as r

    finish = r._finish_completed

    def delayed_completion(*args: Any) -> None:
        assert not any(t.name.endswith("-stall-guard") for t in threading.enumerate())
        time.sleep(0.4)
        finish(*args)

    r._finish_completed = delayed_completion


def run_runner(root: Path, schedule_id: int, mode: str) -> None:
    if mode == "missing-module-files":
        import selectors
        import subprocess

        del subprocess.__file__
        del selectors.__file__
    elif mode == "delayed-record":
        _delay_record_error(root)
    elif mode == "delayed-completion":
        _delay_completion()
    import ava
    from base.config import settings
    from services.wake.schedule_manager import (
        engine,  # noqa: F401  # pyright: ignore[reportUnusedImport]
    )
    from services.wake.schedule_manager.runner import run

    ava.ensure_plugins_loaded = lambda **_inputs: None
    settings.gateway.schedule_stall_timeout_seconds = 0.15
    raise SystemExit(run(schedule_id))


def stall_exit(root: Path, blocked_write: str) -> None:
    database, _inputs = _schedule_inputs()
    from services.wake.schedule_manager import engine as r

    r._FAILURE_RECORD_DEADLINE_S = 1.0

    def record_error(*_args: Any) -> None:
        (root / "started").write_text(str(time.monotonic()))
        time.sleep(60 if blocked_write == "error" else 0.7)

    def record_run_end(*_args: Any, **_kwargs: Any) -> None:
        (root / "second-write").touch()
        threading.Event().wait()

    r._record_error = record_error
    r._record_run_end = record_run_end
    r._stall_action(database(), 1, "test stall", 1)


def _ignore(*_args: Any) -> None:
    return None


def completion_during_stall(root: Path) -> None:
    """The script returns while a real stall recorder is blocked; hard exit still wins."""

    database, inputs = _schedule_inputs()
    import ava
    from services.wake.schedule_manager import engine as r

    r.settings.gateway.schedule_stall_timeout_seconds = 0.04
    r._FAILURE_RECORD_DEADLINE_S = 1.0
    r._schedule_dir = lambda _id: root
    r._load = lambda *_args: ("pass", "python script.py")
    r._record_run_start = lambda *_args: 1
    r.base.host.proc.kill_process_tree = lambda *_args, **_kwargs: None  # pyright: ignore[reportAttributeAccessIssue]
    r._StallGuard._sample = lambda _self: ("script.py", 1, "blocked")  # pyright: ignore[reportAttributeAccessIssue]
    ava.ensure_plugins_loaded = lambda: None

    def record_error(*_args: Any) -> None:
        (root / "started").write_text(str(time.monotonic()))
        time.sleep(60)

    def script(*_args: Any, **_kwargs: Any) -> None:
        while not (root / "started").exists():
            time.sleep(0.005)
        (root / "script-returned").touch()

    r._record_error = record_error
    r.runpy.run_path = script  # pyright: ignore[reportAttributeAccessIssue]
    r._finish_completed = lambda *_args: (root / "completed").touch()
    raise SystemExit(
        r._run(
            database(),
            1,
            bind_actor=_ignore,
            load_plugins=ava.ensure_plugins_loaded,
            inputs=inputs,
        )
    )


scenario, *arguments = sys.argv[1:]
if scenario == "run":
    run_runner(Path(arguments[0]), int(arguments[1]), arguments[2])
elif scenario == "stall_exit":
    stall_exit(Path(arguments[0]), arguments[1])
elif scenario == "completion_during_stall":
    completion_during_stall(Path(arguments[0]))
else:
    raise ValueError(f"unknown scenario {scenario!r}")
