"""Finite retained executor. It never edits source or launches application units.

A fleet operation runs the coordinator (`cli.release_fleet.coordinator`), a
remote unit's operation its follower, and a PITR operation its own phases.
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path

from pydantic import JsonValue

from cli.release_fleet.request import FleetRequest
from cli.release_transition.failure import OperationFailure, failure_detail
from cli.release_transition.journal import Journal, Operation, exclusive, read_operation
from cli.release_transition.request import PitrRequest
from shared.release_operation import executor_heartbeat


def execute(path: Path) -> None:
    operation = read_operation(path)
    request = operation.request
    os.environ["AVA_HOME"] = request.home
    os.environ["AVA_CLUSTER_REGISTRY"] = request.registry
    from shared.runtime_interpreter import verify_loaded_image

    image = request.executor.verify(Path(request.home))
    verify_loaded_image(
        Path(request.home),
        image,
        schema_digest=request.executor.schema_digest,
        source_commit=request.executor.source_commit,
    )
    if operation.launch is None or not operation.launch_attempted:
        raise RuntimeError("release executor has no recorded native launch attempt")
    with exclusive(path) as journal:
        # The receipt is computed and recorded under the lock for the launch
        # this process was started for; a relaunch in between refuses.
        if journal.operation.launch != operation.launch:
            raise RuntimeError("release executor belongs to a retired native launch attempt")
        journal.record_native(_executor_receipt(operation.launch))
        # The health probe trusts the journal to explain an outage only while
        # this stamp is fresh (shared.release_operation.operation_in_flight).
        with executor_heartbeat(path):
            _drive(journal, path)


def _drive(journal: Journal, path: Path) -> None:
    request = journal.operation.request
    if isinstance(request, PitrRequest):
        from cli.release_transition.pitr.inputs import require_inputs
        from shared.release_operation import authorized_pitr

        require_inputs(journal.operation)
        with authorized_pitr(path, journal.pitr_record_write):
            drive_pitr(journal)
    elif isinstance(request, FleetRequest):
        from cli.release_fleet.coordinator import run_coordinator
        from cli.release_transition.authority import adopt_executor_authority

        adopt_executor_authority(Path(request.home))
        run_coordinator(journal)
    else:
        from cli.release_fleet.follower import run_follower

        run_follower(journal)


def _executor_receipt(launch: dict[str, JsonValue]) -> dict[str, JsonValue]:
    """Prove this process is the recorded executor of its own native adapter kind."""
    from cli.release_transition import native

    if native.recorded_kind(launch) == native.DARWIN:
        from cli.release_transition.launcher_macos import executor_receipt

        return executor_receipt(launch)
    from cli.release_transition.launcher_linux import readback

    job = readback(launch)
    if job.owner is None or job.owner.pid != os.getpid() or not job.owner.live():
        raise RuntimeError("this process is not the recorded external executor")
    return {
        "unit": job.unit,
        "boot_id": job.boot_id,
        "invocation_id": job.invocation_id,
        "cgroup": job.cgroup,
        "pid": job.owner.pid,
        "birth": job.owner.birth,
        "starttime": job.owner.starttime,
    }


def reenter(operation: Operation) -> None:
    """Replace this interpreter using only retained native launch inputs."""
    from cli.release_transition.launcher_linux import LinuxLaunch, readback

    if operation.launch is None:
        raise RuntimeError("sealed reentry lacks native launch evidence")
    launch = LinuxLaunch.model_validate(operation.launch)
    native = readback(operation.launch)
    if native.owner is None or native.owner.pid != os.getpid() or not native.owner.live():
        raise RuntimeError("sealed reentry requires the same native executor birth")
    os.chdir(launch.cwd)
    os.execve(launch.interpreter, launch.argv, launch.environment)  # noqa: S606 — retained verified native argv
    raise RuntimeError("native exec unexpectedly returned")


def drive_pitr(journal: Journal) -> None:
    from cli.release_transition.pitr.transition import PitrTransition, persist_failure

    request = journal.operation.request
    if not isinstance(request, PitrRequest):
        raise TypeError("PITR execution requires its typed request")
    driver = PitrTransition(request)
    while not journal.operation.terminal:
        operation = journal.operation
        try:
            match operation.phase:
                case "prepared":
                    driver.preflight()
                    journal.advance("provisioning")
                case "provisioning":
                    if driver.provision(journal):
                        reenter(journal.operation)
                case "quiescing":
                    driver.quiesce(operation)
                    journal.advance("stopping_apps")
                case "stopping_apps":
                    driver.stop_apps(journal)
                    journal.advance("stopping_data")
                case "stopping_data":
                    driver.stop_data(operation)
                    journal.advance("starting")
                case "starting":
                    driver.start(operation)
                    journal.advance("observing")
                case "observing":
                    driver.observe(operation)
                    journal.advance("resuming")
                case "resuming":
                    driver.resume(operation)
                    journal.advance("proving")
                case "proving":
                    driver.prove(journal)
                    journal.advance("complete")
                case _:
                    raise ValueError("unknown PITR execution phase")
        except OperationFailure as exc:
            journal.fail(failure_detail(exc))
            persist_failure(journal.operation, exc)
            raise


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--operation", type=Path, required=True)
    execute(parser.parse_args().operation)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
