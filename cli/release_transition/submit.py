"""Admit one prepared request and dispatch its recorded external executor."""

from __future__ import annotations

import json
import sys
from pathlib import Path

from pydantic import JsonValue

from cli.release_transition.journal import Operation, create, exclusive, read_operation
from cli.release_transition.local import LocalTransition
from cli.release_transition.request import PitrRequest, Request, read_request
from shared.verified_file import regular_bytes


def _retire_previous(request: Request | PitrRequest) -> None:
    """An active-pointer replacement cannot orphan the prior native executor."""
    from cli.release_transition import native

    try:
        path = Path(regular_bytes(Path(request.home) / "updates" / "active").decode().strip())
    except FileNotFoundError:
        return
    prior = read_operation(path)
    if prior.request.home != request.home:
        raise ValueError("active operation belongs to another home")
    if prior.request.id != request.id and prior.terminal and prior.launch is not None:
        native.for_launch(prior.launch).retire_current(prior.launch)


def submit(path: Path) -> tuple[Path, dict[str, JsonValue]]:
    """Repeated submissions inspect the same operation, never dispatch twice."""
    return submit_request(read_request(regular_bytes(path)))


def _terminal_native(operation: Operation) -> dict[str, JsonValue]:
    from cli.release_transition import native

    if not operation.launch_attempted:
        return {"state": "not_started"}
    if operation.launch is None:
        raise ValueError("attempted operation has no retained launch intent")
    return (
        native.for_launch(operation.launch).retire_current(operation.launch).model_dump(mode="json")
    )


def submit_request(request: Request | PitrRequest) -> tuple[Path, dict[str, JsonValue]]:
    """Reserve and dispatch one typed home operation, or join its retained attempt."""
    from cli.release_transition import native
    from shared.paths import ava_home

    if Path(request.home) != ava_home():
        raise ValueError("prepared request belongs to a different configured home")
    host = native.for_host(request)
    if request.path.exists():
        existing = read_operation(request.path)
        if existing.request != request:
            raise ValueError("operation id already belongs to different release inputs")
        if existing.terminal:
            return request.path, _terminal_native(existing)
        if existing.launch_attempted:
            if existing.launch is None:
                raise ValueError("attempted operation has no retained launch intent")
            adapter = native.for_launch(existing.launch)
            if existing.retirement is not None:
                job = adapter.resume(existing.launch)
            else:
                job = adapter.readback(existing.launch)
                if job.finished:
                    job = adapter.resume(existing.launch)
            return request.path, job.model_dump(mode="json")
        if existing.attempt > 0:
            # Recover a controller death after retiring the old native job but
            # before recording or submitting the next attempt. Inputs and
            # release phase remain the original operation's captured decision.
            runtime = request.executor.verify(Path(request.home), request.platform_tag)
            record = existing.launch or host.plan_launch(request.path, runtime)
            with exclusive(request.path) as journal:
                journal.record_launch(record)
            return request.path, native.for_launch(record).launch(record).model_dump(mode="json")
    if isinstance(request, PitrRequest):
        from cli.release_transition.pitr import PitrTransition

        pitr = PitrTransition(request)
        pitr.preflight()
        image = pitr.image
    else:
        driver = LocalTransition(request)
        driver.preflight()
        image = driver.candidate
    _retire_previous(request)
    create(request)
    record = host.plan_launch(request.path, image)
    with exclusive(request.path) as journal:
        journal.record_launch(record)
    return request.path, host.launch(record).model_dump(mode="json")


def run(path: Path) -> int:
    try:
        operation_path, native = submit(path)
    except (ValueError, OSError, RuntimeError) as exc:
        sys.stderr.write(f"release update refused: {exc}\n")
        return 2
    operation = read_operation(operation_path)
    sys.stdout.write(
        json.dumps(
            {
                "operation": str(operation_path),
                "phase": operation.phase,
                "direction": operation.direction,
                "native": native,
            }
        )
        + "\n"
    )
    return 0
