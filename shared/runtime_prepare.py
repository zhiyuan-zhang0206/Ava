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
from typing import cast

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


def _copy_verified_python(source: Path, target: Path, expected: dict[str, str]) -> None:
    _copy_python(source, target)
    if tree_inventory(target) != expected:
        raise ReleaseRejectedError("retained Python bytes differ from trusted input inventory")


def _python_input_inventory(source: Path) -> dict[str, str]:
    """File links are supported; directory links have no finite tree contract here."""
    for path in source.rglob("*"):
        if path.is_symlink():
            if not path.resolve(strict=True).is_relative_to(source):
                raise ReleaseRejectedError("Python bundle has an escaping symlink")
            if path.is_dir():
                raise ReleaseRejectedError("Python input directory symlinks are unsupported")
    return {
        p.relative_to(source).as_posix(): file_sha256(p)
        for p in sorted(source.rglob("*"))
        if p.is_file()
    }


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


def loaded_native_images(root: Path) -> list[str]:
    """Prove declared capabilities and record their actual loaded native images.

    This deliberately does not emulate ELF/dyld lookup or promise every optional
    dlopen path. The real loader receives the retained interpreter's context.
    """
    probe = """
import ctypes, importlib, json, os, pathlib, platform, sys
from unittest.mock import patch
with patch('socket.socket.connect', side_effect=RuntimeError('network forbidden')), \\
     patch('socket.socket.connect_ex', side_effect=RuntimeError('network forbidden')), \\
     patch('socket.create_connection', side_effect=RuntimeError('network forbidden')):
    for name in ('cli.main', 'agent.exec_child', 'ops.spec', 'services.agent_host.daemon', 'gateway.app'):
        importlib.import_module(name)
    import numpy as np, faiss, pyarrow as pa, psycopg.pq
    vectors = np.array([[1.0, 2.0]], dtype='float32')
    index = faiss.IndexFlatL2(2)
    index.add(vectors)
    distances, identifiers = index.search(vectors, 1)
    assert identifiers[0, 0] == 0 and distances[0, 0] == 0
    assert np.dot(vectors, vectors.T)[0, 0] == 5
    table = pa.table({'n': [1, 2]})
    sink = pa.BufferOutputStream()
    with pa.ipc.new_stream(sink, table.schema) as writer:
        writer.write_table(table)
    assert pa.ipc.open_stream(sink.getvalue()).read_all().equals(table)
    assert psycopg.pq.version() > 0
    import _distutils_hack
    assert pathlib.Path(_distutils_hack.__file__).resolve().is_relative_to(pathlib.Path(sys.prefix).resolve())
    image = pathlib.Path(sys.prefix).resolve().parent
    assert all(pathlib.Path(item).resolve().is_relative_to(image) for item in sys.path if item)
if platform.system() == 'Darwin':
    dyld = ctypes.CDLL(None)
    dyld._dyld_image_count.restype = ctypes.c_uint32
    dyld._dyld_get_image_name.argtypes = [ctypes.c_uint32]
    dyld._dyld_get_image_name.restype = ctypes.c_char_p
    images = [os.fsdecode(dyld._dyld_get_image_name(i)) for i in range(dyld._dyld_image_count())]
else:
    images = []
    for line in pathlib.Path('/proc/self/maps').read_text().splitlines():
        fields = line.split(maxsplit=5)
        if len(fields) == 6 and 'x' in fields[1] and fields[5].startswith('/'):
            images.append(fields[5].replace('\\\\040', ' '))
print(json.dumps(sorted(set(images))))
"""
    images = json.loads(_run([str(root / "venv/bin/python"), "-I", "-B", "-c", probe], root))
    if not isinstance(images, list) or not images:
        raise ReleaseRejectedError("loaded-image receipt is empty or invalid")
    allowed = (
        (Path("/usr/lib"), Path("/System/Library"))
        if platform.system() == "Darwin"
        else (Path("/usr/lib"), Path("/lib"), Path("/lib64"))
    )
    result: list[str] = []
    for name in cast(list[object], images):
        if not isinstance(name, str) or not Path(name).is_absolute():
            raise ReleaseRejectedError("invalid loaded native image path")
        path = Path(name).resolve()
        if not path.is_relative_to(root) and not any(path.is_relative_to(p) for p in allowed):
            raise ReleaseRejectedError(f"loaded native image escaped generation/OS ABI: {name}")
        result.append(str(path))
    return sorted(set(result))


@dataclass(frozen=True)
class FrontendInput:
    """A prebuilt, privately copied standalone bundle and its trusted inventory."""

    root: Path
    digest: str


@dataclass(frozen=True)
class CollectorInput:
    """Pinned collector download result; config and durable queues are excluded."""

    root: Path
    digest: str


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
    frontend: FrontendInput | None = None
    otel: CollectorInput | None = None


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


def _retain_startup_wheel(wheels: Path, root: Path) -> None:
    """Keep original locked dependency bytes for the active startup-hook gate."""
    candidates = list(wheels.glob("setuptools-*.whl"))
    if len(candidates) != 1:
        raise ReleaseRejectedError("expected exactly one locked setuptools wheel")
    source = candidates[0]
    destination = root / "wheel-evidence/setuptools.whl"
    destination.parent.mkdir(mode=0o700)
    expected = file_sha256(source)
    shutil.copy2(source, destination)
    if file_sha256(destination) != expected or file_sha256(source) != expected:
        raise ReleaseRejectedError("setuptools evidence changed during private copy")


def _frontend_input_inventory(frontend: FrontendInput | None) -> dict[str, str] | None:
    if frontend is None:
        return None
    files = tree_inventory(frontend.root)
    if inventory_digest(files) != frontend.digest:
        raise ReleaseRejectedError("frontend input hash mismatch")
    return files


def _copy_frontend(
    frontend: FrontendInput | None, root: Path, expected: dict[str, str] | None
) -> None:
    if frontend is None:
        return
    shutil.copytree(frontend.root, root / "frontend", symlinks=False)
    if tree_inventory(root / "frontend") != expected:
        raise ReleaseRejectedError("retained frontend differs from trusted input inventory")


def _collector_input_inventory(collector: CollectorInput | None) -> dict[str, str] | None:
    if collector is None:
        return None
    files = tree_inventory(collector.root)
    if inventory_digest(files) != collector.digest:
        raise ReleaseRejectedError("collector input hash mismatch")
    return files


def _copy_collector(
    collector: CollectorInput | None, root: Path, expected: dict[str, str] | None
) -> None:
    if collector is None:
        return
    shutil.copytree(collector.root, root / "otel", symlinks=False)
    if tree_inventory(root / "otel") != expected:
        raise ReleaseRejectedError("retained collector differs from trusted input inventory")


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
    python_files = _python_input_inventory(source)
    if inventory_digest(python_files) != inputs.python_digest:
        raise ReleaseRejectedError("Python input hash mismatch")
    if file_sha256(inputs.requirements) != inputs.requirements_digest:
        raise ReleaseRejectedError("requirements hash mismatch")
    frontend_files, otel_files = (
        _frontend_input_inventory(inputs.frontend),
        _collector_input_inventory(inputs.otel),
    )
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
            "frontend": "absent" if inputs.frontend is None else inputs.frontend.digest,
            "otel": "absent" if inputs.otel is None else inputs.otel.digest,
        }
    )
    root = store / identity
    root.mkdir(mode=0o700)  # Never reuse a partial or previously sealed generation.
    _copy_frontend(inputs.frontend, root, frontend_files)
    _copy_collector(inputs.otel, root, otel_files)
    _retain_startup_wheel(wheels, root)
    _copy_verified_python(source, root / "python", python_files)
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
    _write_json(root / "loaded-native-images.json", loaded_native_images(root))
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


def verify_loaded_images(release: VerifiedRelease) -> None:
    """Recheck application closure, without attesting trusted host OS bytes."""
    expected = json.loads((release.root / "loaded-native-images.json").read_text(encoding="utf-8"))
    if loaded_native_images(release.root) != expected:
        raise ReleaseRejectedError("native dependency closure changed")
