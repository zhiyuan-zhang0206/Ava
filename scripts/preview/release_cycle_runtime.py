"""Fresh source-interpreter actions for the completed-work image cycle.

The stdlib controller never loads Settings. This adapter is entered only with
its explicit preview home/registry; every image effect uses verified bytes.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from dotenv import dotenv_values

from cli.release_transition.journal import Operation, read_operation
from cli.release_transition.launcher_linux import LinuxJob, readback, retire_current
from cli.release_transition.request import ReleaseRef, Request, verify_pair
from scripts.preview import local
from scripts.preview.linux_observer import _require_context
from scripts.preview.linux_runtime import bound_runtime
from shared.runtime_release import VerifiedRelease, activate_release, current_pointer
from shared.verified_file import regular_bytes

_FIXTURE = """import hashlib, json, sys
from pathlib import Path
from tests.e2e.fakes import _chat_model
from tests.e2e.fakes.scenarios import message_flow
root = Path(sys.argv[1])
files = {}
for module in (_chat_model, message_flow):
    path = Path(module.__file__).resolve(strict=True)
    if not path.is_relative_to(root):
        raise RuntimeError('Scripted fixture imported outside captured image')
    files[module.__name__] = hashlib.sha256(path.read_bytes()).hexdigest()
model = message_flow.build('preview')
first = model.invoke([])
if [(call['name'], call['args']) for call in first.tool_calls] != [('execute_code', {'code': 'print(1 + 2)'})]:
    raise RuntimeError('Fixture did not produce the retained execution scenario')
if model.invoke([]).content != message_flow.REPLY_TEXT:
    raise RuntimeError('Fixture reply differs from its declared scenario')
print(json.dumps({'files': files, 'reply_sha256': hashlib.sha256(message_flow.REPLY_TEXT.encode()).hexdigest()}))
"""


def captured(
    run: Path, receipt: Path, digest: str, commit: str
) -> tuple[ReleaseRef, VerifiedRelease, dict[str, Any]]:
    expected = bound_runtime(run, receipt, digest, commit)
    if expected.image is None or expected.evidence is None:
        raise RuntimeError("image cycle requires an explicit preparation receipt")
    evidence = expected.evidence
    reference = ReleaseRef(
        artifact_digest=evidence["artifact_digest"],
        manifest_digest=evidence["manifest_digest"],
        schema_digest=evidence["schema_digest"],
        source_commit=evidence["source_commit"],
    )
    return reference, expected.image, evidence


def _fixture(run: Path, image: VerifiedRelease) -> dict[str, Any]:
    # Explicit image interpreter and isolated flags; no checkout import path.
    result = subprocess.run(  # noqa: S603 — fixed script and verified interpreter, no shell
        [str(image.interpreter), "-I", "-B", "-X", "utf8", "-c", _FIXTURE, str(image.root)],
        cwd=image.cwd,
        env=local.clean_env(),
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    )
    observed: dict[str, Any] = json.loads(result.stdout)
    for name, digest in observed["files"].items():
        path = run / "source" / (name.replace(".", "/") + ".py")
        if hashlib.sha256(regular_bytes(path)).hexdigest() != digest:
            raise RuntimeError("image and source observer scripted fixtures differ")
    return observed


def prepare(
    run: Path, previous: Path, candidate: Path, bindings: tuple[tuple[str, str], tuple[str, str]]
) -> None:
    from shared.machine import machine_name
    from shared.os_boot_unit import systemd_running, unit_name
    from shared.start_inputs import configuration_digest

    if not systemd_running():
        raise RuntimeError("image cycle requires the native Linux system manager")
    if (
        dotenv_values(run / "home/.env", interpolate=False)["AVA_LLM_OVERRIDE"]
        != local.PROFILE["AVA_LLM_OVERRIDE"]
    ):
        raise RuntimeError("image cycle requires the retained scripted fixture profile")
    destination = run / "release-inputs.json"
    if destination.exists():
        raise ValueError("retained image-cycle inputs already exist")
    a_ref, a_image, a_evidence = captured(run, previous, *bindings[0])
    b_ref, b_image, b_evidence = captured(run, candidate, *bindings[1])
    inputs: dict[str, Any] = {
        "home": str(run / "home"),
        "registry": str(run / "clusters.json"),
        "unit": unit_name(run / "home"),
        "images": {},
        "requests": {},
    }
    if current_pointer(run / "home/releases") is not None:
        raise RuntimeError("initial image cycle requires an unselected source preview")
    for name, receipt, reference, image, evidence in (
        ("a", previous, a_ref, a_image, a_evidence),
        ("b", candidate, b_ref, b_image, b_evidence),
    ):
        inputs["images"][name] = {
            "receipt": str(receipt),
            "reference": reference.model_dump(mode="json"),
            "runtime": evidence,
            "fixture": _fixture(run, image),
        }
    if inputs["images"]["a"]["fixture"] != inputs["images"]["b"]["fixture"]:
        raise RuntimeError("A and B scripted fixture scenarios differ")
    for label, old, new in (("ab", a_ref, b_ref), ("ba", b_ref, a_ref)):
        request = Request(
            id=uuid4(),
            home=str(run / "home"),
            registry=str(run / "clusters.json"),
            created_at=datetime.now(UTC),
            platform_tag=platform.platform(),
            machine=machine_name(),
            previous=old,
            candidate=new,
            executor=new,
            configuration_digest=configuration_digest(run / "home"),
        )
        verify_pair(request)
        path = run / f"release-{label}-request.json"
        path.write_text(request.model_dump_json() + "\n")
        path.chmod(0o600)
        inputs["requests"][label] = {
            "request": str(path),
            "operation": str(request.path),
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        }
    local.write_json(destination, inputs)


def image_input(run: Path, name: str) -> tuple[ReleaseRef, VerifiedRelease]:
    inputs = json.loads(regular_bytes(run / "release-inputs.json"))
    selected = inputs["images"][name]
    reference, image, evidence = captured(
        run,
        Path(selected["receipt"]),
        selected["runtime"]["receipt_sha256"],
        selected["reference"]["source_commit"],
    )
    if (
        evidence != selected["runtime"]
        or reference.model_dump(mode="json") != selected["reference"]
    ):
        raise RuntimeError("captured preparation evidence changed during the cycle")
    return reference, image


def initial(run: Path) -> None:
    from cli.commands._root_driver import _require_root_absent
    from cli.release_transition.root_service import install_steady

    reference, image = image_input(run, "a")
    _require_root_absent()
    activate_release(
        run / "home/releases",
        reference.artifact_digest,
        expected_current=None,
        manifest_digest=reference.manifest_digest,
        platform_tag=platform.platform(),
        schema_digest=reference.schema_digest,
    )
    install_steady(run / "home", run / "clusters.json", reference, image)


def _operation(run: Path, label: str) -> Request:
    encoded = regular_bytes(run / f"release-{label}-request.json")
    inputs = json.loads(regular_bytes(run / "release-inputs.json"))
    if hashlib.sha256(encoded).hexdigest() != inputs["requests"][label]["sha256"]:
        raise RuntimeError("captured cycle request changed")
    request = Request.model_validate_json(encoded)
    if request.home != str(run / "home") or request.registry != str(run / "clusters.json"):
        raise RuntimeError("cycle operation belongs to another preview")
    return request


def dispatch(run: Path, label: str) -> None:
    reference, image = image_input(run, "b" if label == "ab" else "a")
    request = _operation(run, label)
    if request.executor != reference:
        raise RuntimeError("cycle dispatch names a different retained executor")
    subprocess.run(  # noqa: S603 — public CLI in verified image, no shell or service launcher
        image.module_argv(
            "cli.main",
            "cluster",
            "update",
            "--prepared",
            str(run / f"release-{label}-request.json"),
        ),
        cwd=image.cwd,
        env=local.clean_env()
        | {"AVA_HOME": request.home, "AVA_CLUSTER_REGISTRY": request.registry},
        timeout=180,
        check=True,
    )


def _closed_journal(operation: Operation, native: LinuxJob) -> Operation:
    if not native.finished:
        return operation
    final = read_operation(operation.request.path)
    if (
        final.request != operation.request
        or final.attempt != operation.attempt
        or final.launch != operation.launch
    ):
        raise RuntimeError("cycle executor identity changed during native closure observation")
    return final


def _sample(request: Request, *, cleanup: bool) -> tuple[Operation, LinuxJob | None]:
    operation = read_operation(request.path)
    if operation.request != request or operation.attempt != 0:
        raise RuntimeError("cycle operation changed inputs or retried a failed executor")
    if operation.launch is None or not operation.launch_attempted:
        if cleanup:
            return operation, None
        raise RuntimeError("submitted operation has no native dispatch")
    if cleanup and operation.retirement is not None and operation.retirement.state == "absent":
        from cli.release_transition.launcher_linux import LinuxLaunch, _retirement_absent

        # A→B's completed operation is no longer the active journal after B→A.
        # It needs read-only native absence, never renewed mutation authority.
        if not _retirement_absent(LinuxLaunch.model_validate(operation.launch)):
            raise RuntimeError("retired cycle executor reappeared")
        return operation, LinuxJob.model_validate(operation.retirement.terminal)
    native = (
        retire_current(operation.launch)
        if cleanup and operation.retirement is not None
        else readback(operation.launch)
    )
    return _closed_journal(operation, native), native


def _completed(operation: Operation, native: LinuxJob, *, cleanup: bool) -> bool:
    if not native.finished:
        return False
    if cleanup:
        if operation.launch is None:
            raise RuntimeError("native closure lost its launch intent")
        if operation.retirement is None or operation.retirement.state != "absent":
            retire_current(operation.launch)
    else:
        if (
            not operation.terminal
            or operation.direction != "candidate"
            or operation.error is not None
        ):
            raise RuntimeError(
                "finite executor did not complete the requested candidate transition"
            )
        if native.result != "success" or native.exit_code != 1 or native.exit_status != 0:
            raise RuntimeError("finite executor exited unsuccessfully")
    return True


def wait_executor(run: Path, label: str, *, cleanup: bool = False) -> None:
    request = _operation(run, label)
    if cleanup and not request.path.exists():
        return
    deadline = time.monotonic() + (90 if cleanup else 900)
    evidence: dict[str, Any] = {"operation": str(request.path), "samples": []}
    destination = run / f"release-{label}-{'cleanup' if cleanup else 'completion'}.json"
    try:
        while True:
            operation, native = _sample(request, cleanup=cleanup)
            if native is None:
                return
            evidence["samples"].append(
                {
                    "at": time.time(),
                    "phase": operation.phase,
                    "direction": operation.direction,
                    "error": operation.error,
                    "native": native.model_dump(mode="json"),
                }
            )
            local.write_json(destination, evidence)
            if _completed(operation, native, cleanup=cleanup):
                return
            if time.monotonic() >= deadline:
                raise TimeoutError("finite executor remains live; no destroy or retry authorized")  # noqa: TRY301 — retain timeout evidence before propagation
            time.sleep(0.5)
    except BaseException as exc:
        evidence["error"] = repr(exc)
        raise
    finally:
        local.write_json(destination, evidence)


def retired(run: Path, label: str) -> None:
    operation = read_operation(_operation(run, label).path)
    if (
        not operation.terminal
        or operation.direction != "candidate"
        or operation.retirement is None
        or operation.retirement.state != "absent"
    ):
        raise RuntimeError("completed executor lacks exact native retirement")
    local.write_json(run / f"release-{label}-retired.json", operation.model_dump(mode="json"))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run", type=Path)
    parser.add_argument(
        "action",
        choices=(
            "prepare",
            "initial",
            "submit",
            "wait",
            "retired",
            "settle",
            "freeze",
            "state",
            "capture",
            "closed",
        ),
    )
    parser.add_argument("--previous", type=Path)
    parser.add_argument("--candidate", type=Path)
    parser.add_argument("--previous-digest")
    parser.add_argument("--previous-commit")
    parser.add_argument("--candidate-digest")
    parser.add_argument("--candidate-commit")
    parser.add_argument("--label")
    args = parser.parse_args()
    if sys.platform != "linux":
        raise RuntimeError("release cycle is Linux-only")
    run = args.run.resolve(strict=True)
    _require_context(run)
    if args.action == "prepare":
        if any(
            value is None
            for value in (
                args.previous,
                args.candidate,
                args.previous_digest,
                args.previous_commit,
                args.candidate_digest,
                args.candidate_commit,
            )
        ):
            parser.error(
                "prepare requires both explicit receipts and captured digest/commit bindings"
            )
        prepare(
            run,
            args.previous.resolve(strict=True),
            args.candidate.resolve(strict=True),
            (
                (args.previous_digest, args.previous_commit),
                (args.candidate_digest, args.candidate_commit),
            ),
        )
    elif args.action == "initial":
        initial(run)
    elif args.action == "settle":
        if (run / "release-inputs.json").exists():
            for label in ("ab", "ba"):
                wait_executor(run, label, cleanup=True)
    elif args.action in {"freeze", "state", "capture", "closed"}:
        from scripts.preview.release_cycle_custody import capture, closed
        from scripts.preview.release_cycle_state import freeze, verify

        if args.label not in {"source", "a", "b", "a-return"}:
            parser.error("state observation requires a captured cycle phase label")
        {"freeze": freeze, "state": verify, "capture": capture, "closed": closed}[args.action](
            run, args.label
        )
    else:
        if args.label not in {"ab", "ba"}:
            parser.error("transition requires ab or ba label")
        {"wait": wait_executor, "retired": retired, "submit": dispatch}[args.action](
            run, args.label
        )


if __name__ == "__main__":
    main()
