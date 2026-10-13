"""Runs a process logging init against an unreachable gateway; never imported by a production entry.

Run as ``python -I -B _log_init_child.py <repo-root> <init_cli_process|init_gateway_process>``.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, sys.argv[1])
import httpx

from base.host.env import bootstrap


def unreachable(*_args: object, **_kwargs: object) -> None:
    raise httpx.ConnectError("connection refused")


bootstrap.fetch_bootstrap_config = unreachable  # type: ignore[assignment]
import base.telemetry.process_logging  # noqa: E402

assert "base.config" not in sys.modules
pipelines: list[object] = []


def producer() -> object:
    from base.telemetry.delivery.pipeline import EventPipeline

    pipeline = EventPipeline(writer=lambda _batch: None)
    pipelines.append(pipeline)
    return pipeline


try:
    if sys.argv[2] == "init_cli_process":
        base.telemetry.process_logging.init_cli_process(
            name="probe",
            producer=producer,  # type: ignore[arg-type]
            machine_reader=lambda: "probe-host",
        )
    else:
        from base.native_process.loaded_commit import LoadedCommit

        base.telemetry.process_logging.init_gateway_process(
            name="probe",
            producer=producer,  # type: ignore[arg-type]
            machine_reader=lambda: "probe-host",
            image=LoadedCommit(Path(sys.argv[1]), None),
        )
finally:
    for pipeline in pipelines:
        pipeline.stop(timeout=1)  # type: ignore[attr-defined]
