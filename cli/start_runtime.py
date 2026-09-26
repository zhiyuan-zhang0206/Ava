"""Explicit executable identity for the single start lifecycle.

The release operator supplies captured image facts, never a moving selector.
Admission verifies bytes and the interpreter/modules actually executing this
code. It does not grant migration, writer closure, or release publication rights.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

from shared import runtime_interpreter
from shared.runtime_interpreter import LoadedRuntimeIdentity
from shared.runtime_release import (
    ReleaseRejectedError,
    VerifiedRelease,
    current_pointer,
    verify_release,
)


@dataclass(frozen=True)
class StartRuntime:
    code_root: Path
    cwd: Path
    interpreter: Path
    release: VerifiedRelease | None = None
    home: Path | None = None
    source_commit: str | None = None
    schema_digest: str | None = None

    @classmethod
    def development(cls, checkout: Path) -> StartRuntime:
        from shared.runtime_interpreter import WHEEL_RUNTIME

        if WHEEL_RUNTIME:
            raise ReleaseRejectedError(
                "installed start requires explicit verified release admission"
            )
        return cls(checkout, checkout, Path(sys.executable).absolute())

    @classmethod
    def from_image(
        cls, home: Path, image: VerifiedRelease, *, schema_digest: str, source_commit: str
    ) -> StartRuntime:
        """Construct start paths from verified loaded bytes, before selection admission."""
        identity = runtime_interpreter.verify_loaded_image(
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

    def identity(self, home: Path) -> LoadedRuntimeIdentity:
        if self.release is None:
            return runtime_interpreter.verify_loaded_source(self.code_root)
        if self.schema_digest is None or self.source_commit is None or self.home != home:
            raise ReleaseRejectedError("release start runtime lacks its exact home identity")
        return runtime_interpreter.verify_loaded_image(
            home, self.release, schema_digest=self.schema_digest, source_commit=self.source_commit
        )

    def module_argv(self, module: str, *arguments: str) -> list[str]:
        if self.release is not None:
            return list(self.release.module_argv(module, *arguments))
        return [str(self.interpreter), "-m", module, *arguments]

    def validate(self, home: Path) -> None:
        """Recheck a retained image before a lifecycle phase consumes its paths."""
        if self.release is None:
            selector = home / "releases/current-release"
            if selector.exists() or selector.is_symlink():
                raise ReleaseRejectedError(
                    "selected release home cannot start from development source"
                )
            if self != self.development(self.code_root):
                raise ReleaseRejectedError("development start runtime changed")
            return
        if self.home != home or self.schema_digest is None or self.source_commit is None:
            raise ReleaseRejectedError("release start runtime belongs to another home")
        current = admit_release(
            home, self.release, schema_digest=self.schema_digest, source_commit=self.source_commit
        )
        if current != self:
            raise ReleaseRejectedError("captured release runtime changed")


def admit_release(
    home: Path, image: VerifiedRelease, *, schema_digest: str, source_commit: str
) -> StartRuntime:
    """Admit the selected loaded image for an already initialized, exact home."""
    from cli.start_identity import read_intent

    runtime = StartRuntime.from_image(
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


def admit_loaded_release(home: Path) -> StartRuntime:
    """Capture the selected image only when it is the code already executing.

    The embedded identity supplies the commit/schema facts for verification;
    the selected manifest digest authenticates those bytes. This cannot switch
    an installed caller to a different selected image or authorize migration.
    """
    import platform

    from shared.release_identity import ApplicationIdentity
    from shared.verified_file import regular_bytes

    selected = current_pointer(home / "releases")
    if selected is None:
        raise ReleaseRejectedError("installed restart requires a selected verified release")
    digest, manifest_digest = selected
    prefix, _executable, package, _isolated = runtime_interpreter.loaded_runtime()
    if prefix != home / "releases" / digest / "venv" or not package.is_relative_to(prefix):
        raise ReleaseRejectedError("installed restart differs from the selected loaded image")
    identity = ApplicationIdentity.model_validate_json(
        regular_bytes(package / "shared" / "release-build.json")
    )
    image = verify_release(
        home / "releases",
        digest,
        manifest_digest=manifest_digest,
        platform_tag=platform.platform(),
        schema_digest=identity.schema_digest,
    )
    return admit_release(
        home, image, schema_digest=identity.schema_digest, source_commit=identity.source_commit
    )
