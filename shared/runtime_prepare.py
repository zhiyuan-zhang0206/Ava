"""Offline POSIX generation preparation, not a deployment entry point.

The caller supplies trusted artifact hashes. Nothing selects a serving release,
starts a service, migrates data, downloads a dependency, or edits a checkout.
Failed generations are retained for explicit inspection rather than reused.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import shutil
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path

from shared.runtime_release import (
    ReleaseRejectedError,
    VerifiedRelease,
    file_sha256,
    verify_release,
)


def tree_inventory(root: Path) -> dict[str, str]:
    """Hash private regular files; reject links and special files, including FIFOs."""
    result: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        info = path.lstat()
        if stat.S_ISDIR(info.st_mode):
            continue
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise ReleaseRejectedError(f"non-private runtime member: {path.name}")
        result[path.relative_to(root).as_posix()] = file_sha256(path)
    return result


def inventory_digest(files: dict[str, str]) -> str:
    """Stable identity for a complete input inventory, not installed shebang bytes."""
    return hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest()


def _write_json(path: Path, value: object) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        json.dump(value, stream, sort_keys=True)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())


def _run(argv: list[str], cwd: Path, *, timeout: int = 180) -> str:
    # No inherited AVA_HOME, credentials, PYTHONPATH, uv configuration or indexes.
    result = subprocess.run(  # noqa: S603 — verified local artifacts, argv without a shell.
        argv,
        cwd=cwd,
        env={
            "PATH": "/usr/bin:/bin",
            "HOME": str(cwd),
            "UV_NO_CONFIG": "1",
            "UV_OFFLINE": "1",
            "AVA_CONFIG_FETCH": "skip",
            "AVA_TIMEZONE": "UTC",
            "AVA_HOME": str(cwd / "probe-home"),
            "AVA_DB_URL": "postgresql://unused@127.0.0.1:1/unused",
            "AVA_REDIS_URL": "redis://127.0.0.1:1/0",
        },
        text=True,
        capture_output=True,
        timeout=timeout,
        check=False,
    )
    if result.returncode:
        # Dependency URLs/credentials must not leak from subprocess diagnostics.
        raise ReleaseRejectedError(
            f"preparation command failed: {Path(argv[0]).name} rc={result.returncode}"
        )
    return result.stdout


def _copy_python(source: Path, target: Path) -> None:
    """Private-copy a managed Python tree, dereferencing only in-tree symlinks."""
    for path in source.rglob("*"):
        if path.is_symlink() and not path.resolve(strict=True).is_relative_to(source):
            raise ReleaseRejectedError("Python bundle has an escaping symlink")
        if not path.is_symlink() and not (path.is_file() or path.is_dir()):
            raise ReleaseRejectedError("Python bundle contains a special file")
    shutil.copytree(source, target, symlinks=False)


def _materialize_venv_links(root: Path) -> None:
    # stdlib venv creates lib64 -> lib on Linux even with --copies.
    for path in sorted(root.rglob("*")):
        if not path.is_symlink():
            continue
        resolved = path.resolve(strict=True)
        if not resolved.is_relative_to(root):
            raise ReleaseRejectedError("venv contains an external symlink")
        path.unlink()
        if resolved.is_dir():
            shutil.copytree(resolved, path)
        else:
            shutil.copy2(resolved, path)


def native_dependencies(root: Path) -> list[str]:
    """Reject application libraries outside the image; declare trusted OS ABI.

    Kernel/system libraries are platform prerequisites, not release artifacts.
    Optional user plugins/dlopen paths are not covered by this initial adapter.
    Only trusted, hash-verified build inputs may reach the platform loader tools.
    """
    external: set[str] = set()
    darwin = platform.system() == "Darwin"
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        # Retained stdlib bytes are inventoried, but optional extension modules
        # are not all runtime capabilities. Inspect the executable and installed
        # application dependencies; declared boot imports validate their stdlib
        # closure. In particular, ldd(_tkinter) omits its executable's RPATH:
        # https://github.com/astral-sh/python-build-standalone/issues/742
        if path.is_relative_to(root / "python") and path.parent != root / "python/bin":
            continue
        with path.open("rb") as stream:
            magic = stream.read(4)
            if magic not in (
                b"\x7fELF",
                b"\xcf\xfa\xed\xfe",
                b"\xfe\xed\xfa\xcf",
                b"\xca\xfe\xba\xbe",
            ):
                continue
        output = _run(
            ["/usr/bin/otool", "-L", str(path)] if darwin else ["/usr/bin/ldd", str(path)], root
        )
        own_ids = (
            set(_run(["/usr/bin/otool", "-D", str(path)], root).splitlines()[1:])
            if darwin
            else set()
        )
        for line in output.splitlines()[1:] if darwin else output.splitlines():
            fields = line.strip().split()
            if not fields or fields[0].startswith("linux-vdso") or line.endswith(":"):
                continue
            if "not found" in line:
                raise ReleaseRejectedError(
                    f"unresolved native library {line.strip()!r} in {path.relative_to(root)}"
                )
            name = fields[2] if not darwin and len(fields) > 2 and fields[1] == "=>" else fields[0]
            if darwin:
                name = line.strip().split(" (compatibility version", 1)[0]
                if name in own_ids:
                    continue  # LC_ID_DYLIB names this image, not a dependency.
            if darwin and name.startswith(("/usr/lib/", "/System/Library/")):
                external.add(name)  # dyld shared-cache entries may have no disk file.
                continue
            name = name.replace("@loader_path", str(path.parent)).replace(
                "@executable_path", str(root / "python/bin")
            )
            if name.startswith("@rpath/"):
                # Managed CPython/wheels normally use loader-relative links.
                # Unknown search-path contracts are not silently accepted.
                raise ReleaseRejectedError(
                    f"unresolved Mach-O rpath {name!r} in {path.relative_to(root)}"
                )
            dependency = Path(name)
            if not dependency.is_absolute() or not dependency.is_file():
                raise ReleaseRejectedError(
                    f"unrecognized native dependency {name!r} in {path.relative_to(root)}"
                )
            dependency = dependency.resolve(strict=True)
            if not dependency.is_relative_to(root):
                if not darwin and any(
                    dependency.is_relative_to(prefix)
                    for prefix in (Path("/usr/lib"), Path("/lib"), Path("/lib64"))
                ):
                    external.add(str(dependency))
                else:
                    raise ReleaseRejectedError("native application dependency escaped generation")
    if not external:
        raise ReleaseRejectedError("native dependency receipt is empty")
    return sorted(external)


@dataclass(frozen=True)
class PrepareInputs:
    """Trusted build receipt; all paths are local, verified inputs."""

    python_tree: Path
    python_digest: str
    wheelhouse: Path
    wheelhouse_digest: str
    requirements: Path
    requirements_digest: str
    application_wheel: str
    schema_digest: str
    uv: Path


def _optional_stdlib_receipt(source: Path, root: Path, interpreter: Path) -> None:
    probe = """
import json
try:
    import _tkinter
except ImportError as exc:
    print(json.dumps({'available': False, 'reason': str(exc)}))
else:
    print(json.dumps({'available': True}))
"""
    optional = {
        label: json.loads(_run([str(binary), "-I", "-B", "-c", probe], root))
        for label, binary in (
            ("input", source / "bin/python3"),
            ("retained", root / "python/bin/python3"),
            ("venv", interpreter),
        )
    }
    _write_json(root / "optional-stdlib.json", optional)
    if optional["input"]["available"] != optional["retained"]["available"]:
        raise ReleaseRejectedError("retained Python changed optional tkinter availability")


def prepare_release(store: Path, inputs: PrepareInputs) -> VerifiedRelease:
    """Create one final inactive generation or fail without touching serving state.

    Linux ELF and macOS Mach-O are the initial adapters. Other platforms
    fail before filesystem mutation. A future official prepare caller must add
    its rollout lease, disk budget, recovery-point and compatibility gates.
    """
    if platform.system() not in {"Linux", "Darwin"}:
        raise ReleaseRejectedError("native closure preparation requires Linux or macOS")
    if store.is_symlink() or not store.is_dir() or store.resolve() != store.absolute():
        raise ReleaseRejectedError("store must be an existing canonical private directory")
    if stat.S_IMODE(store.stat().st_mode) & 0o077:
        raise ReleaseRejectedError("preparation store must have mode 0700")
    source = inputs.python_tree.resolve(strict=True)
    wheels = inputs.wheelhouse.resolve(strict=True)
    if inventory_digest(tree_inventory(wheels)) != inputs.wheelhouse_digest:
        raise ReleaseRejectedError("wheelhouse hash mismatch")
    # Hash dereferenced files for the source bundle while refusing escaping links.
    for path in source.rglob("*"):
        if path.is_symlink() and not path.resolve(strict=True).is_relative_to(source):
            raise ReleaseRejectedError("Python bundle has an escaping symlink")
    python_files = {
        p.relative_to(source).as_posix(): file_sha256(p)
        for p in sorted(source.rglob("*"))
        if p.is_file()
    }
    if inventory_digest(python_files) != inputs.python_digest:
        raise ReleaseRejectedError("Python input hash mismatch")
    if file_sha256(inputs.requirements) != inputs.requirements_digest:
        raise ReleaseRejectedError("requirements hash mismatch")
    requirements = inputs.requirements.read_text(encoding="utf-8")
    if (
        "://" in requirements
        or " @ " in requirements
        or any(
            line.lstrip().startswith("-") and not line.lstrip().startswith("--hash=sha256:")
            for line in requirements.splitlines()
        )
    ):
        raise ReleaseRejectedError("requirements contain nonlocal install directives")
    if Path(
        inputs.application_wheel
    ).name != inputs.application_wheel or not inputs.application_wheel.endswith(".whl"):
        raise ReleaseRejectedError("application wheel must be a basename")
    wheel = wheels / inputs.application_wheel
    if not wheel.is_file() or not inputs.uv.is_absolute() or not inputs.uv.is_file():
        raise ReleaseRejectedError("application wheel or absolute uv executable missing")
    identity = inventory_digest(
        {
            "python": inputs.python_digest,
            "wheels": inputs.wheelhouse_digest,
            "requirements": inputs.requirements_digest,
            "schema": inputs.schema_digest,
            "application": inputs.application_wheel,
            "platform": platform.platform(),
        }
    )
    root = store / identity
    root.mkdir(mode=0o700)  # Never reuse a partial or previously sealed generation.
    _copy_python(source, root / "python")
    python = root / "python/bin/python3"
    _run(
        [str(python), "-I", "-B", "-m", "venv", "--copies", "--without-pip", str(root / "venv")],
        root,
    )
    interpreter = root / "venv/bin/python"
    for args in (
        ["--require-hashes", "-r", str(inputs.requirements.resolve())],
        ["--no-deps", str(wheel)],
    ):
        _run(
            [
                str(inputs.uv),
                "--no-cache",
                "pip",
                "install",
                "--python",
                str(interpreter),
                "--offline",
                "--no-index",
                "--find-links",
                str(wheels),
                "--link-mode",
                "copy",
                *args,
            ],
            root,
            timeout=600,
        )
    _materialize_venv_links(root / "venv")
    _optional_stdlib_receipt(source, root, interpreter)
    # Ensure the retained interpreter/stdlib, not a mutable Homebrew/system Python,
    # supplies base_prefix. Prove real service imports with all sockets denied.
    facts = json.loads(
        _run(
            [
                str(interpreter),
                "-I",
                "-B",
                "-c",
                "import json,sys,sysconfig;print(json.dumps([sys.base_prefix,sysconfig.get_path('stdlib')]))",
            ],
            root,
        )
    )
    if not all(Path(value).resolve().is_relative_to(root / "python") for value in facts):
        raise ReleaseRejectedError("Python/stdlib escaped retained generation")
    _run(
        [
            str(interpreter),
            "-I",
            "-B",
            "-c",
            """
import hashlib, importlib, importlib.metadata, json, pathlib, sys
from unittest.mock import patch
with patch('socket.socket.connect', side_effect=RuntimeError('network forbidden')), \\
     patch('socket.socket.connect_ex', side_effect=RuntimeError('network forbidden')), \\
     patch('socket.create_connection', side_effect=RuntimeError('network forbidden')):
    for name in ('cli.main', 'agent.exec_child', 'ops.spec', 'services.agent_host.daemon', 'gateway.app'):
        module = importlib.import_module(name)
        assert pathlib.Path(module.__file__).resolve().is_relative_to(pathlib.Path(sys.prefix).resolve()), name
distribution = importlib.metadata.distribution('ava')
schema = pathlib.Path(str(distribution.locate_file('db/schema.sql')))
assert hashlib.sha256(schema.read_bytes()).hexdigest() == sys.argv[1], 'schema mismatch'
""",
            inputs.schema_digest,
        ],
        root,
    )
    if (
        inventory_digest(tree_inventory(wheels)) != inputs.wheelhouse_digest
        or file_sha256(inputs.requirements) != inputs.requirements_digest
    ):
        raise ReleaseRejectedError("inputs changed during preparation")
    _write_json(root / "native-closure.json", native_dependencies(root))
    manifest = {
        "version": 1,
        "artifact_digest": identity,
        "platform": platform.platform(),
        "schema_digest": inputs.schema_digest,
        "interpreter": "venv/bin/python",
        "cwd": "venv",
        "files": tree_inventory(root),
    }
    _write_json(root / "manifest.json", manifest)
    release = verify_release(
        store,
        identity,
        manifest_digest=file_sha256(root / "manifest.json"),
        platform_tag=platform.platform(),
        schema_digest=inputs.schema_digest,
    )
    # Read-only sealing is defense against accidental writes, not a same-UID
    # security boundary. Lifecycle ownership separation remains a later gate.
    for path in root.rglob("*"):
        path.chmod(0o500 if path.is_dir() or os.access(path, os.X_OK) else 0o400)
    root.chmod(0o500)
    return release


def verify_native_closure(release: VerifiedRelease) -> None:
    """Recheck application closure, without attesting trusted host OS bytes."""
    expected = json.loads((release.root / "native-closure.json").read_text(encoding="utf-8"))
    if native_dependencies(release.root) != expected:
        raise ReleaseRejectedError("native dependency closure changed")
