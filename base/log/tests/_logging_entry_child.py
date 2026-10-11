"""Fresh-interpreter logging entry scenarios; never imported by a production entry.

Run as ``python -I -B _logging_entry_child.py <scenario> <repo-root> [role]``. The last JSON line
on stdout is the observation checked by the parent.
"""

from __future__ import annotations

import importlib.abc
import json
import runpy
import sys
from pathlib import Path

from loguru import logger


def emit(text: object) -> None:
    sys.stdout.write(f"{text}\n")


def import_and_embedded_dispatch() -> None:
    logger.remove()
    records: list[str] = []
    sink = logger.add(lambda message: records.append(message.record["message"]), diagnose=False)
    logger.configure(extra={"caller": "retained"})
    import base.log

    assert base.log.logger is logger
    from cli.main import main

    logger.info("after import")
    for _ in range(2):
        try:
            main(["--help"])
        except SystemExit as exc:
            assert exc.code == 0
    logger.info("after embedded dispatch")
    logger.remove(sink)
    emit(json.dumps({"records": records}))


def fresh_entry() -> None:
    records: list[str] = []
    logger.add(lambda message: records.append(message.record["message"]), diagnose=False)
    from cli import main

    # A public dispatch stub observes the actual executable prelude.
    def dispatch() -> int:
        logger.warning("must be quiet before configuration")
        return 17

    main.main = dispatch  # type: ignore[assignment]
    result = main.entry()
    emit(
        json.dumps(
            {
                "records": records,
                "result": result,
                "otel": sorted(name for name in sys.modules if name.startswith("opentelemetry")),
            }
        )
    )


def role_initialization(role: str, root: str) -> None:
    from base.log import logger as base_logger
    from base.log import prepare_process_logging

    prepare_process_logging()
    records: list[str] = []
    agent_ids: list[object] = []

    def observe(message: object) -> None:
        records.append(message.record["message"])  # type: ignore[attr-defined]
        agent_ids.append(message.record["extra"]["agent_id"])  # type: ignore[attr-defined]

    sink = base_logger.add(observe, diagnose=False)
    from base.log.process import init_cli_process, init_gateway_process, init_subprocess_logger
    from base.native_process.loaded_commit import LoadedCommit
    from base.telemetry.delivery.pipeline import EventPipeline

    rows: list[object] = []
    pipeline = EventPipeline(writer=rows.extend)

    def producer() -> EventPipeline:
        return pipeline

    def machine_reader() -> str:
        return "entry-test"

    image = LoadedCommit(Path(root), None)
    if role == "subprocess":
        init_subprocess_logger(agent_id=7)
    elif role == "cli":
        init_cli_process(name="entry-test", producer=producer, machine_reader=machine_reader)
    else:
        init_gateway_process(
            name="entry-test", image=image, producer=producer, machine_reader=machine_reader
        )
    init_cli_process(name="ignored", producer=producer, machine_reader=machine_reader)
    init_subprocess_logger(agent_id=99)
    init_gateway_process(
        name="ignored", image=image, producer=producer, machine_reader=machine_reader
    )
    base_logger.info("after repeated initialization", event="label_change")
    result = pipeline.stop(timeout=2)
    base_logger.remove(sink)
    emit(
        json.dumps(
            {
                "marker_count": records.count("after repeated initialization"),
                "agent": agent_ids[-1],
                "drained": result.status.value,
            }
        )
    )


def no_agent_child() -> None:
    records: list[str] = []
    logger.add(lambda message: records.append(message.record["message"]), diagnose=False)
    try:
        runpy.run_module("agent.execution.child", run_name="__main__")
    except SystemExit as exc:
        assert exc.code == 2
    logger.warning("must stay out of exec output after the no-request refusal")
    emit(
        json.dumps(
            {
                "records": records,
                "loaded": sorted(
                    name
                    for name in sys.modules
                    if name.startswith("opentelemetry") or name == "base.telemetry"
                ),
            }
        )
    )


def fresh_ops() -> None:
    records: list[str] = []
    logger.add(lambda message: records.append(message.record["message"]), diagnose=False)

    class ConfigurationReachedError(Exception):
        pass

    class ConfigurationBoundary(importlib.abc.MetaPathFinder):
        def find_spec(self, fullname, path=None, target=None):  # type: ignore[no-untyped-def]
            if fullname == "base.config":
                raise ConfigurationReachedError

    sys.meta_path.insert(0, ConfigurationBoundary())
    sys.argv = ["agent-ops"]
    try:
        runpy.run_module("services.agent_runner.agent_ops.daemon", run_name="__main__")
    except ConfigurationReachedError:
        reached = True
    else:
        reached = False
    logger.warning("must be quiet before process initialization")
    emit(json.dumps({"reached": reached, "records": records}))


if __name__ == "__main__":
    scenario, root = sys.argv[1], sys.argv[2]
    sys.path.insert(0, root)
    if scenario == "role_initialization":
        role_initialization(sys.argv[3], root)
    else:
        {
            "import_and_embedded_dispatch": import_and_embedded_dispatch,
            "fresh_entry": fresh_entry,
            "no_agent_child": no_agent_child,
            "fresh_ops": fresh_ops,
        }[scenario]()
