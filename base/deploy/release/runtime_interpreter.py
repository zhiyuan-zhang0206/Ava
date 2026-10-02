"""Interpreter paths for the currently imported source checkout, never a moving selector.

The checkout keeps its own venv (`.venv`).
"""

from __future__ import annotations

import hashlib
import stat
import subprocess
import sys
from pathlib import Path
from typing import Literal, Self

from pydantic import model_validator

from base.native_process.evidence import Digest, EvidenceModel


def runtime_venv(*, checkout: Path | None = None) -> Path:
    """Return the current checkout's environment or an explicitly targeted checkout."""
    if checkout is not None:
        return checkout / ".venv"
    from base.paths import repo_root

    return repo_root() / ".venv"


def external_plugin_read_root() -> Path:
    """Shared discovery source; never use this as an installer destination."""
    from base.paths import plugins_dir

    return plugins_dir()


class LoadedRuntimeError(ValueError):
    """The loaded modules are not one canonical installation of this checkout."""


_STABLE_STAT = ("st_dev", "st_ino", "st_mode", "st_size", "st_mtime_ns", "st_ctime_ns")


def source_digest(repo: Path) -> str:
    """Bind the names, modes and bytes Git would include, without changing Git."""
    head = subprocess.check_output(  # noqa: S603 — fixed read-only Git argv, canonical checkout
        ["git", "-C", str(repo), "rev-parse", "HEAD"], timeout=30
    )
    result = subprocess.run(  # noqa: S603 — fixed read-only Git argv, no shell
        ["git", "-C", str(repo), "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        check=True,
        capture_output=True,
        timeout=30,
    )
    names = sorted(set(result.stdout.split(b"\0")) - {b""})
    digest = hashlib.sha256(head)
    for encoded in names:
        path = repo / encoded.decode("utf-8")
        digest.update(encoded + b"\0")
        try:
            before = path.lstat()
            mode = before.st_mode
        except FileNotFoundError:
            digest.update(b"deleted\0")
            continue
        if stat.S_ISLNK(mode):
            content = str(path.readlink()).encode()
        elif stat.S_ISREG(mode):
            content = path.read_bytes()
        else:
            raise RuntimeError(f"unsupported development source member: {path}")
        after = path.lstat()
        if any(getattr(before, key) != getattr(after, key) for key in _STABLE_STAT):
            raise RuntimeError(f"development source changed while reading: {path}")
        digest.update(str(mode).encode() + b"\0" + hashlib.sha256(content).digest())
    if (
        subprocess.check_output(  # noqa: S603 — same read-only HEAD check
            ["git", "-C", str(repo), "rev-parse", "HEAD"], timeout=30
        )
        != head
    ):
        raise RuntimeError("development HEAD changed while reading source")
    return digest.hexdigest()


class LoadedRuntimeIdentity(EvidenceModel):
    """Loaded code identity; no selector, publication or maintenance permission.

    Only ``kind="source"`` is produced; ``"release"`` remains in the persisted shape.
    """

    kind: Literal["source", "release"]
    code_root: str
    interpreter: str
    prefix: str
    cwd: str
    source_digest: Digest | None = None
    artifact_digest: Digest | None = None
    manifest_digest: Digest | None = None
    schema_digest: Digest | None = None
    source_commit: str | None = None

    @model_validator(mode="after")
    def explicit_origin(self) -> Self:
        for value in (self.code_root, self.interpreter, self.prefix, self.cwd):
            if not Path(value).is_absolute() or str(Path(value)) != value:
                raise ValueError("runtime paths must be explicit absolute paths")
        release = (
            self.artifact_digest,
            self.manifest_digest,
            self.schema_digest,
            self.source_commit,
        )
        if self.kind == "source":
            if self.source_digest is None or any(value is not None for value in release):
                raise ValueError("source runtime requires only its captured source digest")
        elif self.source_digest is not None or any(value is None for value in release):
            raise ValueError("release runtime requires complete captured artifact identity")
        return self


def loaded_runtime() -> tuple[Path, Path, Path, bool]:
    """Read actual imports without importing Settings or a higher application layer."""
    # base/deploy/release/runtime_interpreter.py -> the import root (checkout or site-packages).
    package = Path(__file__).resolve().parents[3]
    for name in ("base", "cli", "ava", "agent", "gateway", "services"):
        module = sys.modules.get(name)
        if (
            module is not None
            and module.__file__ is not None
            and Path(module.__file__).resolve().parent.parent != package
        ):
            raise LoadedRuntimeError("loaded modules belong to different installations")
    return (
        Path(sys.prefix).resolve(),
        Path(sys.executable).resolve(),
        package,
        bool(sys.flags.isolated and sys.flags.dont_write_bytecode),
    )


def verify_loaded_source(checkout: Path) -> LoadedRuntimeIdentity:
    """Capture the exact developer checkout and the interpreter loading it."""
    prefix, executable, package, _isolated = loaded_runtime()
    if (
        checkout.resolve(strict=True) != checkout
        or package != checkout
        or package.is_relative_to(prefix)
    ):
        raise LoadedRuntimeError("development runtime differs from its loaded canonical checkout")
    return LoadedRuntimeIdentity(
        kind="source",
        code_root=str(package),
        interpreter=str(executable),
        prefix=str(prefix),
        cwd=str(checkout),
        source_digest=source_digest(checkout),
    )


def capture_loaded_runtime() -> LoadedRuntimeIdentity:
    """Capture the checkout that is executing this code."""
    _prefix, _executable, package, _isolated = loaded_runtime()
    return verify_loaded_source(package)
