"""Export verified raw packages as standalone inputs to another acquisition."""

from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import stat
import sys
import tempfile
from pathlib import Path

from cli.release_prepare.acquisition_models import Acquisition, AcquisitionReceipt
from cli.release_prepare.inputs import require_directory
from cli.release_prepare.models import FileInput, TreeInput, encode
from shared.atomic_io import fsync_parent
from shared.platform import file_lock
from shared.runtime_prepare import inventory_digest, tree_inventory
from shared.runtime_release import ReleaseRejectedError
from shared.verified_file import regular_bytes


def validate_distributions(seed: TreeInput) -> None:
    """Only a captured flat inventory of regular package files is reusable."""
    require_directory(seed.root)
    files = list(seed.root.iterdir())
    if not files or any(not stat.S_ISREG(path.lstat().st_mode) for path in files):
        raise ReleaseRejectedError("source distributions require a nonempty flat regular tree")
    if inventory_digest(tree_inventory(seed.root)) != seed.digest:
        raise ReleaseRejectedError("source distribution inventory changed")


def _capture(receipt_file: FileInput) -> AcquisitionReceipt:
    # Keep validation/export independent of package execution and avoid an
    # acquire -> input validation -> acquire import cycle.
    from cli.release_prepare.acquire import verify_acquisition

    if receipt_file.path.resolve(strict=True) != receipt_file.path:
        raise ReleaseRejectedError("acquisition receipt must have a canonical path")
    encoded = regular_bytes(receipt_file.path, max_bytes=16 * 1024 * 1024)
    if hashlib.sha256(encoded).hexdigest() != receipt_file.digest:
        raise ReleaseRejectedError("acquisition receipt bytes changed")
    receipt = AcquisitionReceipt.model_validate_json(encoded)
    request = Acquisition.model_validate_json(regular_bytes(receipt.request.work / "request.json"))
    if request != receipt.request:
        raise ReleaseRejectedError("acquisition receipt differs from retained request")
    verify_acquisition(receipt)
    validate_distributions(receipt.source_distributions)
    return receipt


def _publish(source: TreeInput, store: Path) -> TreeInput:
    target = TreeInput(root=store / source.digest, digest=source.digest)
    with file_lock(store / "publication.lock", timeout_s=5):
        if target.root.exists() or target.root.is_symlink():
            validate_distributions(target)
            return target
        stage = Path(tempfile.mkdtemp(prefix=".capture-", dir=store))
        try:
            for path in sorted(source.root.iterdir()):
                copied = stage / path.name
                shutil.copyfile(path, copied)
                copied.chmod(0o400)
                with copied.open("rb") as stream:
                    os.fsync(stream.fileno())
            validate_distributions(TreeInput(root=stage, digest=source.digest))
            validate_distributions(source)
            stage.chmod(0o500)
            fsync_parent(stage / "inventory")
            stage.rename(target.root)
            fsync_parent(target.root)
        finally:
            if stage.exists():
                stage.chmod(0o700)
                shutil.rmtree(stage)
    return target


def export_distributions(receipt_file: FileInput, store: Path) -> TreeInput:
    """Copy successful acquisition inputs without retaining its run or image."""
    require_directory(store)
    if stat.S_IMODE(store.stat().st_mode) != 0o700:
        raise ReleaseRejectedError("distribution input store must have mode 0700")
    receipt = _capture(receipt_file)
    source = receipt.source_distributions
    if store.is_relative_to(source.root) or source.root.is_relative_to(store):
        raise ReleaseRejectedError("distribution store overlaps acquisition inputs")
    # A store inside a producing run would disappear with that run.
    if store.is_relative_to(receipt.request.work) or store.is_relative_to(receipt.request.repo):
        raise ReleaseRejectedError("distribution store must be outside acquisition and checkout")
    return _publish(source, store)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--receipt", type=Path, required=True)
    parser.add_argument("--receipt-sha256", required=True)
    parser.add_argument("--store", type=Path, required=True)
    args = parser.parse_args()
    try:
        result = export_distributions(
            FileInput(path=args.receipt, digest=args.receipt_sha256), args.store
        )
    except (OSError, RuntimeError, ValueError) as exc:
        sys.stderr.write(f"distribution export failed: {exc}\n")
        return 1
    sys.stdout.write(encode(result).decode())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
