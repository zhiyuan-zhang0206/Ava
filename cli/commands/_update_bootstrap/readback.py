"""Read-only bindings between a challenged bootstrap, image, and native identity."""

from pathlib import Path

from services.agent_ops.bootstrap import BootstrapRuntimeIdentity, PreparedObservation
from shared.managed_writer_observation import observe_process
from shared.runtime_release import ReleaseRejectedError, VerifiedRelease, file_sha256


def verify_image_binding(context: PreparedObservation, image: VerifiedRelease) -> None:
    expected_root = Path(context.expected.home) / "releases" / context.expected.artifact_digest
    if (
        image.root != expected_root
        or image.root.resolve(strict=True) != expected_root
        or image.digest != context.expected.artifact_digest
        or image.manifest_digest != context.expected.manifest_digest
        or file_sha256(image.root / "manifest.json") != image.manifest_digest
    ):
        raise ReleaseRejectedError("bootstrap image differs from the verified invocation")


def verify_runtime_binding(
    context: PreparedObservation,
    image: VerifiedRelease,
    raw: dict[str, object],
    identity: BootstrapRuntimeIdentity,
) -> None:
    if (
        raw["mode"] != "bootstrap_observation"
        or raw["full_ready"] is not False
        or raw["challenge"] != str(context.challenge.challenge)
        or raw["unit"] != context.expected.unit().model_dump(mode="json")
        or not isinstance(raw["observer_instance"], str)
        or not raw["observer_instance"]
        or identity.home != context.expected.home
        or identity.artifact_digest != context.expected.artifact_digest
        or identity.manifest_digest != context.expected.manifest_digest
        or not Path(identity.module).is_relative_to(image.root / "venv")
        or Path(identity.module).resolve(strict=True) != Path(identity.module)
        or observe_process(identity.process) != "alive"
    ):
        raise ReleaseRejectedError("bootstrap endpoint returned another runtime identity")
