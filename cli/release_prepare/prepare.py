"""Bind the existing committed wheel build to one inactive verified image."""

from __future__ import annotations

import hashlib
import json
import os
import platform
import shutil
import tarfile
import zipfile
from pathlib import Path

from pydantic import Field

from cli.release_build import (
    ApplicationBuild,
    build_application,
)
from cli.release_prepare.inputs import combine_wheels, validate_inputs, validate_paths
from cli.release_prepare.models import (
    BuildEvidence,
    Digest,
    ImageEvidence,
    Preparation,
    PreparationReceipt,
    encode,
)
from shared.runtime_prepare import (
    CollectorInput,
    FrontendInput,
    PluginInput,
    PrepareInputs,
    prepare_release,
)
from shared.runtime_release import (
    ApplicationIdentity,
    ReleaseRejectedError,
    VerifiedRelease,
    file_sha256,
    read_application_identity,
    verify_release,
)
from shared.verified_file import regular_bytes


class _BuildReceipt(ApplicationIdentity):
    wheel: str = Field(min_length=1)
    wheel_digest: Digest
    build_constraints_digest: Digest


def _write(path: Path, encoded: bytes) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(encoded)
        stream.flush()
        os.fsync(stream.fileno())


def _source_lock(archive: Path) -> str:
    with tarfile.open(archive) as source:
        members = [member for member in source.getmembers() if member.name == "uv.lock"]
        if len(members) != 1 or not members[0].isfile():
            raise ReleaseRejectedError("committed source requires one regular uv.lock")
        contents = source.extractfile(members[0])
        if contents is None:
            raise ReleaseRejectedError("committed source lock is unreadable")
        return hashlib.sha256(contents.read()).hexdigest()


def _verify_build_receipt(
    request: Preparation, build: ApplicationBuild, receipt: _BuildReceipt, directory: Path
) -> None:
    if (
        receipt.source_commit != request.commit
        or receipt.source_archive_digest != file_sha256(directory / "source.tar")
        or build.source_commit != receipt.source_commit
        or build.schema_digest != receipt.schema_digest
        or build.applied_names != receipt.applied_names
        or build.wheel != directory / "wheels" / receipt.wheel
        or Path(receipt.wheel).name != receipt.wheel
        or build.wheel_digest != receipt.wheel_digest
        or file_sha256(build.wheel) != receipt.wheel_digest
        or receipt.build_constraints_digest != request.inputs.build_constraints.digest
    ):
        raise ReleaseRejectedError("application build differs from captured source receipt")


def _built_source(request: Preparation, build: ApplicationBuild) -> tuple[ApplicationIdentity, str]:
    directory = request.work / "application"
    encoded = regular_bytes(directory / "build-receipt.json")
    receipt = _BuildReceipt.model_validate_json(encoded)
    if encoded != encode(receipt):
        raise ReleaseRejectedError("application build receipt is not canonical")
    _verify_build_receipt(request, build, receipt, directory)
    if _source_lock(directory / "source.tar") != request.inputs.source_lock_digest:
        raise ReleaseRejectedError("supplied source lock digest differs from committed archive")
    identity = ApplicationIdentity.model_validate(
        receipt.model_dump(exclude={"wheel", "wheel_digest", "build_constraints_digest"})
    )
    with zipfile.ZipFile(build.wheel) as wheel:
        if wheel.namelist().count("shared/release-build.json") != 1 or wheel.read(
            "shared/release-build.json"
        ) != encode(identity):
            raise ReleaseRejectedError(
                "build receipt differs from the application's embedded source identity"
            )
    return identity, hashlib.sha256(encoded).hexdigest()


def _runtime_inputs(
    request: Preparation, build: ApplicationBuild, wheels: Path, digest: str
) -> PrepareInputs:
    supplied = request.inputs
    requirements = request.work / "requirements.txt"
    shutil.copyfile(supplied.requirements.path, requirements)
    if file_sha256(requirements) != supplied.requirements.digest:
        raise ReleaseRejectedError("supplied requirements changed while copying")
    return PrepareInputs(
        python_tree=supplied.python.root,
        python_digest=supplied.python.digest,
        wheelhouse=wheels,
        wheelhouse_digest=digest,
        requirements=requirements,
        requirements_digest=supplied.requirements.digest,
        application_wheel=build.wheel.name,
        schema_digest=build.schema_digest,
        uv=supplied.uv.path,
        frontend=FrontendInput(supplied.frontend.root, supplied.frontend.digest)
        if supplied.frontend
        else None,
        otel=CollectorInput(supplied.collector.root, supplied.collector.digest)
        if supplied.collector
        else None,
        plugins=PluginInput(
            supplied.plugins.root, supplied.plugins.digest, supplied.required_plugins
        )
        if supplied.plugins
        else None,
    )


def _image(
    request: Preparation, image: VerifiedRelease, source: ApplicationIdentity
) -> ImageEvidence:
    verified = verify_release(
        request.store,
        image.digest,
        manifest_digest=image.manifest_digest,
        platform_tag=platform.platform(),
        schema_digest=source.schema_digest,
    )
    if verified != image or read_application_identity(verified, request.commit) != source:
        raise ReleaseRejectedError("installed application differs from captured build identity")
    return ImageEvidence(
        artifact_digest=image.digest,
        manifest_digest=image.manifest_digest,
        schema_digest=source.schema_digest,
        platform=platform.platform(),
        root=image.root,
        interpreter=image.interpreter,
        cwd=image.cwd,
    )


def _require_work_unchanged(
    request: Preparation,
    captured: bytes,
    build: ApplicationBuild,
    source: ApplicationIdentity,
    receipt_digest: str,
) -> None:
    if regular_bytes(request.work / "request.json") != captured:
        raise ReleaseRejectedError("captured preparation request changed")
    if _built_source(request, build) != (source, receipt_digest):
        raise ReleaseRejectedError("captured application build changed during preparation")


def prepare_image(request: Preparation) -> PreparationReceipt:
    """Prepare from trusted local inputs; leave success or failure evidence in work.

    Work must be new and the private runtime store must already exist. Neither
    failed work nor a partial generation is reused, removed or repaired here.
    """
    validate_paths(request)
    request.work.mkdir(mode=0o700)
    captured = encode(request)
    _write(request.work / "request.json", captured)
    phase = "validate-inputs"
    try:
        validate_inputs(request.inputs)
        phase = "build-application"
        build = build_application(
            request.repo,
            request.commit,
            request.work / "application",
            uv=request.inputs.uv.path,
            python=request.inputs.python.root / "bin/python3",
            cache_dir=request.inputs.cache_dir,
            build_constraints=request.inputs.build_constraints.path,
        )
        source, receipt_digest = _built_source(request, build)
        phase = "combine-inputs"
        wheels, wheelhouse_digest = combine_wheels(request, build.wheel, build.wheel_digest)
        inputs = _runtime_inputs(request, build, wheels, wheelhouse_digest)
        validate_inputs(request.inputs)
        phase = "prepare-runtime"
        image = prepare_release(request.store, inputs)
        phase = "verify-image"
        evidence = _image(request, image, source)
        validate_inputs(request.inputs)
        _require_work_unchanged(request, captured, build, source, receipt_digest)
        receipt = PreparationReceipt(
            request=request,
            request_digest=hashlib.sha256(captured).hexdigest(),
            source=source,
            build=BuildEvidence(
                wheel=build.wheel.name,
                wheel_digest=build.wheel_digest,
                receipt_digest=receipt_digest,
                source_lock_digest=request.inputs.source_lock_digest,
                combined_wheelhouse_digest=wheelhouse_digest,
            ),
            image=evidence,
        )
        _write(request.work / "receipt.json", encode(receipt))
        return receipt
    except BaseException as exc:
        try:
            _write(
                request.work / "failed.json",
                (
                    json.dumps(
                        {"phase": phase, "error_type": type(exc).__name__, "error": str(exc)},
                        sort_keys=True,
                    )
                    + "\n"
                ).encode(),
            )
        except OSError as recording:
            exc.add_note(f"could not retain preparation failure: {recording}")
        raise
