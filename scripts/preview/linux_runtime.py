"""Explicit executable expectations for source and retained-image observations.

Preparation is input evidence only. This module never consults the moving
release selector or treats an image receipt as readiness or process custody.
"""

from __future__ import annotations

import hashlib
import json
import platform
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cli.release_prepare import PreparationReceipt
from shared.release_identity import read_application_identity
from shared.runtime_release import VerifiedRelease, verify_release
from shared.session_env import frontend_toolchain_path, normalize_service_path
from shared.verified_file import regular_bytes


@dataclass(frozen=True)
class ExpectedRuntime:
    interpreter: Path
    cwd: Path
    image: VerifiedRelease | None = None
    evidence: dict[str, Any] | None = None

    def argv(self, run: Path) -> list[str]:
        directory = run / "home/run/ava-root"
        arguments = (
            "--run-dir",
            str(directory),
            "--manifests",
            str(directory / "manifests.json"),
            "--wiring",
            "services.ava_root_glue.glue:build_wiring",
        )
        if self.image is not None:
            return list(self.image.module_argv("services.ava_root", *arguments))
        return [str(self.interpreter), "-m", "services.ava_root", *arguments]

    def environment(self, run: Path, declared: str) -> dict[str, str]:
        bindir = self.interpreter.parent
        host_path = normalize_service_path(declared, excluded=(bindir,))
        return {
            "AVA_HOME": str(run / "home"),
            "AVA_CLUSTER_REGISTRY": str(run / "clusters.json"),
            "VIRTUAL_ENV": str(bindir.parent),
            "AVA_SERVICE_PATH": host_path,
            "PATH": normalize_service_path(
                ":".join((str(bindir), host_path, frontend_toolchain_path("")))
            ),
        }


def expected_runtime(run: Path, receipt_path: Path | None) -> ExpectedRuntime:
    """Verify the explicitly captured preparation output, never current-release."""
    if receipt_path is None:
        source = run / "source"
        return ExpectedRuntime(source / ".venv/bin/python", source)
    encoded = regular_bytes(receipt_path, max_bytes=2 * 1024 * 1024)
    receipt = PreparationReceipt.model_validate_json(encoded)
    return _image_runtime(run, receipt, hashlib.sha256(encoded).hexdigest())


def bound_runtime(run: Path, receipt_path: Path, digest: str, commit: str) -> ExpectedRuntime:
    """Read once, bind exact caller-captured bytes/source, then verify that parsed image."""
    encoded = regular_bytes(receipt_path, max_bytes=2 * 1024 * 1024)
    if hashlib.sha256(encoded).hexdigest() != digest:
        raise RuntimeError("captured preparation receipt digest changed")
    receipt = PreparationReceipt.model_validate_json(encoded)
    if receipt.source.source_commit != commit or receipt.request.commit != commit:
        raise RuntimeError("preparation receipt differs from requested source commit")
    return _image_runtime(run, receipt, digest)


def _image_runtime(run: Path, receipt: PreparationReceipt, digest: str) -> ExpectedRuntime:
    if receipt.request.store != run / "home/releases":
        raise RuntimeError("prepared image store belongs to another preview home")
    if receipt.image.platform != platform.platform():
        raise RuntimeError("prepared image platform differs from this native host")
    image = verify_release(
        receipt.request.store,
        receipt.image.artifact_digest,
        manifest_digest=receipt.image.manifest_digest,
        platform_tag=platform.platform(),
        schema_digest=receipt.image.schema_digest,
    )
    if (image.root, image.interpreter, image.cwd) != (
        receipt.image.root,
        receipt.image.interpreter,
        receipt.image.cwd,
    ):
        raise RuntimeError("prepared image paths differ from the verified runtime")
    if read_application_identity(image, receipt.request.commit) != receipt.source:
        raise RuntimeError("prepared image source identity differs from its build receipt")
    return ExpectedRuntime(
        image.interpreter,
        image.cwd,
        image,
        {
            "kind": "image",
            "receipt_sha256": digest,
            "request_digest": receipt.request_digest,
            "source_commit": receipt.source.source_commit,
            **receipt.image.model_dump(mode="json"),
        },
    )


def environment_digest(environment: dict[str, str]) -> str:
    """Report complete native environment comparison evidence without secrets."""
    return hashlib.sha256(json.dumps(environment, sort_keys=True).encode()).hexdigest()
