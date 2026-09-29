"""Acquire production inputs from one captured commit before any runtime outage."""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
import shutil
import sys
import tarfile
from pathlib import Path

from cli.release_build import CapturedSource, capture_source
from cli.release_prepare import acquisition_assets as assets
from cli.release_prepare.acquisition_dependencies import (
    build_environment,
    dependencies,
    file_input,
    managed_python,
    tree_input,
    verify_derivations,
)
from cli.release_prepare.acquisition_models import Acquisition, AcquisitionReceipt
from cli.release_prepare.acquisition_process import Commands
from cli.release_prepare.inputs import validate_inputs
from cli.release_prepare.models import FileInput, LocalInputs, TreeInput, encode
from cli.release_prepare.source_distributions import validate_distributions
from shared.brew_pin import UV_VERSION
from shared.runtime_plugins import declared_plugins
from shared.runtime_release import ReleaseRejectedError, file_sha256
from shared.verified_file import regular_bytes


def _input_paths(request: Acquisition) -> list[Path]:
    paths = [request.repo, request.uv.path, request.build_constraints.path]
    if request.frontend:
        paths.extend((request.frontend.node.path, request.frontend.npm.root))
    if request.plugins:
        paths.append(request.plugins.root)
    if request.source_distributions:
        paths.append(request.source_distributions.root)
    return paths


def _validate(request: Acquisition) -> None:
    paths = _input_paths(request)
    if any(path.resolve(strict=True) != path for path in [request.work.parent, *paths]):
        raise ReleaseRejectedError("acquisition input paths must be canonical")
    if any(
        request.work.is_relative_to(path) or path.is_relative_to(request.work) for path in paths
    ):
        raise ReleaseRejectedError("acquisition work overlaps input source or tools")
    for item in (request.uv, request.build_constraints):
        if file_input(item.path) != item:
            raise ReleaseRejectedError("supplied acquisition tool bytes changed")
    if request.plugins and tree_input(request.plugins.root) != request.plugins:
        raise ReleaseRejectedError("supplied plugin input changed")
    if request.source_distributions:
        validate_distributions(request.source_distributions)
    present = declared_plugins(request.plugins.root) if request.plugins else {}
    if not set(request.required_plugins) <= set(present):
        raise ReleaseRejectedError("requested acquisition plugin is missing")
    if request.frontend:
        assets.validate_frontend_tools(request.frontend)


def _persist(path: Path, encoded: bytes) -> None:
    with path.open("xb") as stream:
        stream.write(encoded)


def _source_inputs(request: Acquisition, captured: CapturedSource) -> dict[str, FileInput]:
    names = ["uv.lock", "pyproject.toml", ".python-version"]
    if request.frontend:
        names.extend(
            (
                "scripts/runtime-node-version",
                "scripts/prepare_frontend_release.mjs",
                "ui/web/package-lock.json",
                "ui/web/package.json",
                "ui/web/next.config.ts",
            )
        )
    if request.collector:
        names.extend(("scripts/prepare_otel_release.py", "shared/collector_artifact.py"))
    return {name: file_input(captured.source / name) for name in names}


def _require_source_inputs(inputs: dict[str, FileInput]) -> None:
    if any(file_input(item.path) != item for item in inputs.values()):
        raise ReleaseRejectedError("captured acquisition source inputs changed")


def _capture_tools(request: Acquisition, commands: Commands) -> tuple[Path, str]:
    constraints = request.work / "build-constraints.txt"
    shutil.copyfile(request.build_constraints.path, constraints)
    if file_input(constraints).digest != request.build_constraints.digest:
        raise ReleaseRejectedError("build constraints changed while capturing")
    uv_version = commands.run([str(request.uv.path), "--version"], request.work)
    if uv_version.split()[:2] != ["uv", UV_VERSION]:
        raise ReleaseRejectedError("acquisition requires the approved uv version")
    return constraints, uv_version


def _plugins(request: Acquisition) -> TreeInput | None:
    plugins = None
    if request.plugins:
        target = request.work / "plugins"
        shutil.copytree(request.plugins.root, target)
        plugins = tree_input(target)
        if plugins.digest != request.plugins.digest:
            raise ReleaseRejectedError("plugins changed while privately copying")
    return plugins


def _verify_capture(
    request: Acquisition, captured: CapturedSource, source_lock: FileInput, inputs: LocalInputs
) -> None:
    _validate(request)
    validate_inputs(inputs)
    if (
        file_input(source_lock.path) != source_lock
        or file_sha256(captured.archive) != captured.identity.source_archive_digest
    ):
        raise ReleaseRejectedError("captured acquisition source changed")
    if regular_bytes(request.work / "request.json") != encode(request):
        raise ReleaseRejectedError("captured acquisition request changed")


def verify_acquisition(receipt: AcquisitionReceipt) -> None:
    """Recheck retained local acquisition evidence; no selector or runtime authority."""
    _validate(receipt.request)
    validate_inputs(receipt.inputs)
    _require_source_inputs(receipt.source_inputs)
    files = [receipt.archive, receipt.source_lock, receipt.exported_requirements]
    files.extend(item.output for item in receipt.commands)
    files.extend(item for edge in receipt.derivations for item in (edge.source, edge.wheel))
    if any(file_input(item.path) != item for item in files):
        raise ReleaseRejectedError("retained acquisition evidence changed")
    if any(
        tree_input(item.root) != item
        for item in (receipt.source_distributions, receipt.build_tools)
    ):
        raise ReleaseRejectedError("retained acquisition inventory changed")
    _verify_archived_inputs(receipt)
    verify_derivations(
        receipt.source_lock.path.parent,
        receipt.source_distributions,
        receipt.inputs.wheelhouse,
        receipt.derivations,
    )


def _verify_archived_inputs(receipt: AcquisitionReceipt) -> None:
    with tarfile.open(receipt.archive.path) as archive:
        for name, item in receipt.source_inputs.items():
            if (
                item.path != receipt.request.work / "captured/source" / name
                or ".." in Path(name).parts
            ):
                raise ReleaseRejectedError("source input escaped the captured archive")
            members = [member for member in archive.getmembers() if member.name == name]
            if len(members) != 1 or not members[0].isfile():
                raise ReleaseRejectedError("source input is not a unique archive file")
            stream = archive.extractfile(members[0])
            if stream is None or hashlib.sha256(stream.read()).hexdigest() != item.digest:
                raise ReleaseRejectedError(
                    "acquisition source input differs from the committed archive"
                )


def acquire_inputs(request: Acquisition) -> AcquisitionReceipt:
    """Retain an exclusive acquisition and verified LocalInputs; never select an image."""
    if platform.system() not in {"Linux", "Darwin"}:
        raise ReleaseRejectedError("input acquisition supports Linux and macOS")
    _validate(request)
    request.work.mkdir(mode=0o700)
    _persist(request.work / "request.json", encode(request))
    commands = Commands(request.work, request.uv.path)
    phase = "capture-source"
    try:
        captured = capture_source(request.repo, request.commit, request.work / "captured")
        source_inputs = _source_inputs(request, captured)
        source_lock = file_input(captured.source / "uv.lock")
        constraints, uv_version = _capture_tools(request, commands)
        phase = "managed-python"
        python, executable, python_version = managed_python(commands, captured.source)
        phase = "build-tools"
        builder, pip_version, build_tools = build_environment(commands, executable, constraints)
        phase = "dependencies"
        _require_source_inputs(source_inputs)
        exported, distributions, requirements, edges = dependencies(
            commands, captured.source, builder, constraints, seed=request.source_distributions
        )
        phase = "assets"
        _require_source_inputs(source_inputs)
        frontend = (
            assets.frontend(commands, captured.source, request.frontend)
            if request.frontend
            else None
        )
        collector = (
            assets.collector(commands, captured.source, executable) if request.collector else None
        )
        plugins = _plugins(request)
        inputs = LocalInputs(
            python=python,
            wheelhouse=tree_input(request.work / "wheels"),
            requirements=requirements,
            source_lock_digest=source_lock.digest,
            build_constraints=file_input(constraints),
            uv=request.uv,
            cache_dir=request.work / "cache",
            frontend=frontend,
            collector=collector,
            plugins=plugins,
            required_plugins=request.required_plugins,
        )
        phase = "verify-inputs"
        _require_source_inputs(source_inputs)
        _verify_capture(request, captured, source_lock, inputs)
        receipt = AcquisitionReceipt(
            request=request,
            source=captured.identity,
            archive=file_input(captured.archive),
            source_lock=source_lock,
            source_inputs=source_inputs,
            exported_requirements=exported,
            source_distributions=distributions,
            build_tools=build_tools,
            derivations=edges,
            commands=tuple(commands.evidence),
            platform=platform.platform(),
            python_version=python_version,
            pip_version=pip_version,
            uv_version=uv_version,
            inputs=inputs,
        )
        verify_acquisition(receipt)
        _persist(request.work / "local-inputs.json", encode(inputs))
        _persist(request.work / "acquisition-receipt.json", encode(receipt))
        return receipt
    except BaseException as exc:
        try:
            _persist(
                request.work / "failed.json",
                (
                    json.dumps(
                        {
                            "phase": phase,
                            "error_type": type(exc).__name__,
                            "error": str(exc),
                            "notes": getattr(exc, "__notes__", []),
                            "commands": [
                                item.model_dump(mode="json") for item in commands.evidence
                            ],
                        },
                        sort_keys=True,
                    )
                    + "\n"
                ).encode(),
            )
        except OSError as recording:
            exc.add_note(f"could not retain acquisition failure: {recording}")
        raise


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--request", required=True, type=Path, help="Explicit Acquisition JSON")
    args = parser.parse_args()
    try:
        receipt = acquire_inputs(Acquisition.model_validate_json(regular_bytes(args.request)))
    except (OSError, RuntimeError, ValueError) as exc:
        sys.stderr.write(f"input acquisition failed: {exc}\n")
        return 1
    sys.stdout.write(encode(receipt).decode())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
