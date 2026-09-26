"""Read and privately combine supplied inputs without acquiring any packages."""

from __future__ import annotations

import email
import shutil
import zipfile
from pathlib import Path

from cli.release_prepare.models import LocalInputs, Preparation
from shared.runtime_plugins import declared_plugins
from shared.runtime_prepare import _python_input_inventory, inventory_digest, tree_inventory
from shared.runtime_release import ReleaseRejectedError, file_sha256


def require_directory(path: Path) -> None:
    if not path.is_dir() or path.resolve(strict=True) != path:
        raise ReleaseRejectedError("preparation directories must be canonical existing directories")


def _overlap(first: Path, second: Path) -> bool:
    return first.is_relative_to(second) or second.is_relative_to(first)


def validate_paths(request: Preparation) -> None:
    """Separate work/store/cache writes from source and supplied artifact bytes."""
    inputs = request.inputs
    for path in (request.repo, request.work.parent, request.store, inputs.cache_dir):
        require_directory(path)
    sources = [
        inputs.python.root,
        inputs.wheelhouse.root,
        inputs.requirements.path,
        inputs.build_constraints.path,
        inputs.uv.path,
    ]
    sources.extend(
        tree.root for tree in (inputs.frontend, inputs.collector, inputs.plugins) if tree
    )
    for source in sources:
        if source.resolve(strict=True) != source:
            raise ReleaseRejectedError("supplied input roots must be canonical paths")
    outputs = (request.work, request.store)
    protected = (request.repo, inputs.cache_dir, *sources)
    if any(_overlap(output, source) for output in outputs for source in protected):
        raise ReleaseRejectedError("preparation outputs overlap source or supplied inputs")
    if _overlap(request.work, request.store):
        raise ReleaseRejectedError("preparation work and runtime store must be separate")
    if any(_overlap(inputs.cache_dir, source) for source in (request.repo, *sources)):
        raise ReleaseRejectedError("build cache overlaps source or supplied inputs")


def validate_inputs(inputs: LocalInputs) -> None:
    for root in (inputs.python.root, inputs.wheelhouse.root):
        require_directory(root)
    if inventory_digest(_python_input_inventory(inputs.python.root)) != inputs.python.digest:
        raise ReleaseRejectedError("supplied Python inventory changed")
    if inventory_digest(tree_inventory(inputs.wheelhouse.root)) != inputs.wheelhouse.digest:
        raise ReleaseRejectedError("supplied wheelhouse inventory changed")
    for item in (inputs.requirements, inputs.build_constraints, inputs.uv):
        if not item.path.is_file() or file_sha256(item.path) != item.digest:
            raise ReleaseRejectedError("supplied requirements or uv bytes changed")
    for tree in (inputs.frontend, inputs.collector, inputs.plugins):
        if tree:
            require_directory(tree.root)
        if tree and inventory_digest(tree_inventory(tree.root)) != tree.digest:
            raise ReleaseRejectedError("supplied asset inventory changed")
    if inputs.plugins and not set(inputs.required_plugins) <= set(
        declared_plugins(inputs.plugins.root)
    ):
        raise ReleaseRejectedError("requested plugin missing from supplied input")


def _dependency_wheel(path: Path) -> None:
    if not path.is_file() or path.suffix != ".whl":
        raise ReleaseRejectedError("dependency wheelhouse must contain only wheel files")
    with zipfile.ZipFile(path) as archive:
        metadata = [
            n for n in archive.namelist() if n.endswith(".dist-info/METADATA") and n.count("/") == 1
        ]
        if len(metadata) != 1:
            raise ReleaseRejectedError("supplied wheel has no unique package identity")
        package = email.message_from_bytes(archive.read(metadata[0]))
    if not package["Name"] or package["Name"].lower() == "ava":
        raise ReleaseRejectedError("dependency inputs must not supply the application wheel")


def combine_wheels(request: Preparation, application: Path, digest: str) -> tuple[Path, str]:
    """Keep input wheelhouses untouched and bind the exact newly built wheel."""
    target = request.work / "wheelhouse"
    target.mkdir(mode=0o700)
    source = request.inputs.wheelhouse.root
    for path in sorted(source.iterdir()):
        _dependency_wheel(path)
        shutil.copyfile(path, target / path.name)
    if inventory_digest(tree_inventory(target)) != request.inputs.wheelhouse.digest:
        raise ReleaseRejectedError("dependency wheels changed while privately copying")
    if (target / application.name).exists():
        raise ReleaseRejectedError("built application collides with a supplied wheel")
    shutil.copyfile(application, target / application.name)
    if file_sha256(application) != digest or file_sha256(target / application.name) != digest:
        raise ReleaseRejectedError("application wheel changed while privately copying")
    return target, inventory_digest(tree_inventory(target))
