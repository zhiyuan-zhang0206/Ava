"""The sole root boot unit's finite, operation-authorized start action."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from cli.release_transition.boot import start_image
from cli.release_transition.journal import Operation, read_operation


def _require_inputs(operation: Operation) -> None:
    if operation.pitr is not None:
        from cli.release_transition.pitr_inputs import require_inputs

        require_inputs(operation)
    else:
        operation.require_configuration()


def start_operation(path: Path) -> int:
    operation = read_operation(path)
    if operation.phase != "starting":
        raise RuntimeError("release start action requires the recorded starting phase")
    request = operation.request
    _require_inputs(operation)
    home = Path(request.home)
    os.environ["AVA_HOME"] = request.home
    os.environ["AVA_CLUSTER_REGISTRY"] = request.registry
    # The updater job may request start; it cannot become the new root's
    # lifetime owner. Existing PID publication also checks ControlPID/birth.
    from shared.os_boot_unit import in_boot_unit

    if not in_boot_unit(home):
        raise RuntimeError("Linux release start must run inside the ordinary root boot unit")
    from shared.release_operation import authorized_start

    release = operation.reference
    with authorized_start(path):
        result = start_image(home, Path(request.registry), release)
        _require_inputs(operation)
        return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--operation", type=Path, required=True)
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--observe", action="store_true")
    action.add_argument("--preflight", action="store_true")
    parser.add_argument("--previous", action="store_true")
    args = parser.parse_args()
    if args.previous and not args.preflight:
        parser.error("--previous requires --preflight")
    if args.preflight:
        return preflight_operation(args.operation, previous=args.previous)
    return observe_operation(args.operation) if args.observe else start_operation(args.operation)


def preflight_operation(path: Path, *, previous: bool = False) -> int:
    """Reject an unbootable selected roster before any operation stops work."""
    operation = read_operation(path)
    if operation.phase not in {"prepared", "quiescing"}:
        raise RuntimeError("release preflight requires a pre-stop operation phase")
    request = operation.request
    _require_inputs(operation)
    os.environ["AVA_HOME"] = request.home
    os.environ["AVA_CLUSTER_REGISTRY"] = request.registry
    from cli.release_transition.request import PitrRequest
    from cli.start_runtime import StartRuntime

    if isinstance(request, PitrRequest):
        if previous:
            raise ValueError("PITR has only one captured image")
        reference = request.image
    else:
        reference = request.previous if previous else request.candidate
    runtime = StartRuntime.from_image(
        Path(request.home),
        reference.verify(Path(request.home), request.platform_tag),
        schema_digest=reference.schema_digest,
        source_commit=reference.source_commit,
    )
    from cli.commands._repo import _services_for_roles_annotated
    from cli.commands._root_driver import _root_child_env, _start_roster, _tree_manifest
    from cli.commands._start_generation import launch_digest
    from shared.machine import machine_role
    from shared.service_selection import resolve_selection

    roles = machine_role()
    names = {spec.session for spec, _reason in _services_for_roles_annotated(roles)}
    disabled = resolve_selection(names, persist=False, publish=False)
    roster = _start_roster(roles, disabled)
    _tree_manifest(roster, runtime.code_root, roles=roles, runtime=runtime)
    digest = launch_digest(
        runtime.code_root,
        _root_child_env(),
        home=Path(request.home),
        runtime=runtime,
    )
    _require_inputs(operation)
    # The manifest contains private child environment values. Emit only the
    # generation digest and selected names; never serialize credentials here.
    print(
        json.dumps(
            {
                "artifact": reference.artifact_digest,
                "launch_digest": digest,
                "services": [spec.session for spec in roster],
            },
            sort_keys=True,
        )
    )
    return 0


def observe_operation(path: Path) -> int:
    """Verify the selected image's own full manifest and fresh health round."""
    operation = read_operation(path)
    if operation.phase not in {"starting", "observing", "resuming"}:
        raise RuntimeError("root observation requires a selected start/observe phase")
    request = operation.request
    _require_inputs(operation)
    os.environ["AVA_HOME"] = request.home
    os.environ["AVA_CLUSTER_REGISTRY"] = request.registry
    from cli.start_runtime import admit_release

    reference = operation.reference
    runtime = admit_release(
        Path(request.home),
        reference.verify(Path(request.home), request.platform_tag),
        schema_digest=reference.schema_digest,
        source_commit=reference.source_commit,
    )
    from cli.commands._repo import _services_for_roles_annotated
    from cli.commands._root_driver import _start_roster, _wait_for_service_tree, admit_live_start
    from shared.machine import machine_role
    from shared.os_boot_unit import _manager_properties, _process_cgroup, unit_name
    from shared.root_control.client import root_process
    from shared.service_selection import resolve_selection

    roles = machine_role()
    names = {spec.session for spec, _reason in _services_for_roles_annotated(roles)}
    disabled = resolve_selection(names, persist=False, publish=False)
    roster = _start_roster(roles, disabled)
    if not admit_live_start(roster, runtime.code_root, roles, reconcile=True, runtime=runtime):
        raise RuntimeError("selected application root is absent")
    root = root_process()
    expected_group = f"/system.slice/{unit_name(Path(request.home))}"
    native = _manager_properties(Path(request.home))
    if (
        root is None
        or native["MainPID"] != str(root.pid)
        or native["ControlPID"] != "0"
        or native["ActiveState"] != "active"
        or native["ControlGroup"] != expected_group
        or _process_cgroup(root.pid) != expected_group
    ):
        raise RuntimeError("selected root is not independently owned by its boot service")
    wait = _wait_for_service_tree(roster, timeout_s=60)
    if wait.unready or wait.non_critical_unready:
        raise RuntimeError("selected root has incomplete service readiness")
    _require_inputs(operation)
    if operation.pitr is not None:
        from cli.release_transition.pitr import observe_postgres

        observe_postgres(operation)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
