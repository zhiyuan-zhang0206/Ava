"""Pinned installed-image entry to the ordinary idempotent start lifecycle.

Also the retained-image `StartRuntime`: the release operator supplies captured
image facts, never a moving selector, and admission verifies bytes and the
interpreter/modules actually executing this code. It does not grant migration,
writer closure, or release publication rights. Production starts
(`cli.start_runtime.StartRuntime`) never import this module.
"""

from __future__ import annotations

import argparse
import os
from dataclasses import dataclass
from pathlib import Path

from base.deploy.release import loaded_image
from base.deploy.release.runtime_interpreter import LoadedRuntimeIdentity
from base.deploy.release.runtime_release import (
    ReleaseRejectedError,
    VerifiedRelease,
    current_pointer,
)
from cli.release_transition.request import ReleaseRef
from cli.start_runtime import StartRuntime


@dataclass(frozen=True)
class ImageStartRuntime(StartRuntime):
    release: VerifiedRelease
    home: Path
    source_commit: str
    schema_digest: str

    @classmethod
    def from_image(
        cls, home: Path, image: VerifiedRelease, *, schema_digest: str, source_commit: str
    ) -> ImageStartRuntime:
        """Construct start paths from verified loaded bytes, before selection admission."""
        identity = loaded_image.verify_loaded_image(
            home, image, schema_digest=schema_digest, source_commit=source_commit
        )
        return cls(
            Path(identity.code_root),
            image.cwd,
            image.interpreter,
            image,
            home,
            source_commit,
            schema_digest,
        )

    def identity(self) -> LoadedRuntimeIdentity:
        return loaded_image.verify_loaded_image(
            self.home,
            self.release,
            schema_digest=self.schema_digest,
            source_commit=self.source_commit,
        )

    def module_argv(self, module: str, *arguments: str) -> list[str]:
        return list(self.release.module_argv(module, *arguments))

    def validate(self) -> None:
        """Recheck a retained image before a lifecycle phase consumes its paths."""
        current = admit_release(
            self.home,
            self.release,
            schema_digest=self.schema_digest,
            source_commit=self.source_commit,
        )
        if current != self:
            raise ReleaseRejectedError("captured release runtime changed")


def admit_release(
    home: Path, image: VerifiedRelease, *, schema_digest: str, source_commit: str
) -> ImageStartRuntime:
    """Admit the selected loaded image for an already initialized, exact home."""
    from cli.start_identity import read_intent

    runtime = ImageStartRuntime.from_image(
        home, image, schema_digest=schema_digest, source_commit=source_commit
    )
    if current_pointer(home / "releases") != (image.digest, image.manifest_digest):
        raise ReleaseRejectedError("release start differs from the selected captured image")
    intent = read_intent(home)
    if (
        intent is None
        or intent["phase"] not in {"provisioned", "ready"}
        or not (home / ".env").is_file()
    ):
        raise ReleaseRejectedError(
            "release start requires an initialized home; preparation is incomplete"
        )
    return runtime


def start_image(home: Path, release: ReleaseRef) -> int:
    """Load Settings only after explicit image, identity and home admission.

    The image's ABI tag is checked against this boot's host: an OS patch or
    kernel update since preparation still boots; an incompatible host refuses.
    """
    from cli.parsers import build_parser
    from cli.start_intent import run_start

    os.environ["AVA_HOME"] = str(home)
    image = release.verify(home)
    runtime = admit_release(
        home, image, schema_digest=release.schema_digest, source_commit=release.source_commit
    )
    args = build_parser().parse_args(["start", "--persist-services"])
    return run_start(args, runtime=runtime)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--home", type=Path, required=True)
    parser.add_argument("--artifact", required=True)
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--schema", required=True)
    parser.add_argument("--commit", required=True)
    args = parser.parse_args()
    release = ReleaseRef(
        artifact_digest=args.artifact,
        manifest_digest=args.manifest,
        schema_digest=args.schema,
        source_commit=args.commit,
    )
    return start_image(args.home, release)


if __name__ == "__main__":
    raise SystemExit(main())
