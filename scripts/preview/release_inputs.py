"""Compose production acquisition and an explicit scripted-fixture image input."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import time
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path

from cli.release_prepare import LocalInputs, Preparation, PreparationReceipt, prepare_image
from cli.release_prepare.acquire import acquire_inputs, verify_acquisition
from cli.release_prepare.acquisition_models import Acquisition, AcquisitionReceipt
from cli.release_prepare.inputs import validate_inputs
from cli.release_prepare.models import FileInput, TreeInput, encode
from scripts.preview.release_fixture import FixtureWheel, build_fixture
from shared.runtime_prepare import inventory_digest, tree_inventory
from shared.runtime_release import ReleaseRejectedError, file_sha256
from shared.verified_file import regular_bytes


def _write(path: Path, value: object) -> None:
    with path.open("x", encoding="utf-8") as stream:
        json.dump(value, stream, sort_keys=True, indent=2)
        stream.write("\n")


def _verify_acquired(acquired: AcquisitionReceipt) -> None:
    verify_acquisition(acquired)
    if regular_bytes(acquired.request.work / "acquisition-receipt.json") != encode(acquired):
        raise ReleaseRejectedError("persisted acquisition receipt changed")


@contextmanager
def _phase(work: Path, name: str) -> Generator[None]:
    started = time.time()
    verdict: dict[str, object] = {"result": "running", "started_at": started}
    try:
        yield
        verdict["result"] = "passed"
    except BaseException as exc:
        verdict.update(result="failed", error=repr(exc))
        raise
    finally:
        verdict["finished_at"] = time.time()
        _write(work / f"{name}-phase.json", verdict)


def derive_inputs(acquired: AcquisitionReceipt, fixture: FixtureWheel, work: Path) -> LocalInputs:
    """Copy admitted dependency bytes; add the proof wheel without altering provenance."""
    _verify_acquired(acquired)
    if file_sha256(fixture.wheel) != fixture.digest:
        raise ReleaseRejectedError("scripted fixture wheel changed before input derivation")
    original = acquired.inputs
    if any(
        name.lower().startswith("ava_preview_fixture-")
        for name in tree_inventory(original.wheelhouse.root)
    ):
        raise ReleaseRejectedError("production acquisition already contains a preview fixture")
    work.mkdir(mode=0o700)
    wheels = work / "wheels"
    shutil.copytree(original.wheelhouse.root, wheels)
    if inventory_digest(tree_inventory(wheels)) != original.wheelhouse.digest:
        raise ReleaseRejectedError("copied production wheels differ from acquisition")
    destination = wheels / fixture.wheel.name
    shutil.copyfile(fixture.wheel, destination)
    if file_sha256(destination) != fixture.digest:
        raise ReleaseRejectedError("copied scripted fixture differs from its captured wheel")
    requirements = work / "requirements.txt"
    production = regular_bytes(original.requirements.path)
    if hashlib.sha256(production).hexdigest() != original.requirements.digest:
        raise ReleaseRejectedError("production requirements changed before derivation")
    requirements.write_bytes(
        production.rstrip(b"\n")
        + f"\nava-preview-fixture==0.0.0 --hash=sha256:{fixture.digest}\n".encode()
    )
    inputs = LocalInputs.model_validate(
        original.model_dump()
        | {
            "wheelhouse": TreeInput(root=wheels, digest=inventory_digest(tree_inventory(wheels))),
            "requirements": FileInput(path=requirements, digest=file_sha256(requirements)),
        }
    )
    _verify_acquired(acquired)
    (work / "local-inputs.json").write_bytes(encode(inputs))
    _write(
        work / "derivation.json",
        {
            "version": 1,
            "purpose": "production acquisition plus an explicit proof-only scripted model",
            "acquisition_receipt": str(acquired.request.work / "acquisition-receipt.json"),
            "acquisition_digest": hashlib.sha256(encode(acquired)).hexdigest(),
            "production_inputs": original.model_dump(mode="json"),
            "fixture": {"wheel": str(fixture.wheel), "digest": fixture.digest},
            "derived_inputs": inputs.model_dump(mode="json"),
            "fixture_resolved_from_source_lock": False,
        },
    )
    return inputs


def _verify_prepared(
    acquired: AcquisitionReceipt,
    fixture: FixtureWheel,
    receipt: PreparationReceipt,
    request_path: Path,
    captured: bytes,
) -> None:
    _verify_acquired(acquired)
    validate_inputs(receipt.request.inputs)
    if file_sha256(fixture.wheel) != fixture.digest:
        raise ReleaseRejectedError("scripted fixture changed during preparation")
    if regular_bytes(request_path) != captured:
        raise ReleaseRejectedError("captured acquisition request changed during preparation")
    if receipt.source != acquired.source:
        raise ReleaseRejectedError("prepared application differs from acquired committed source")


def prepare_inputs(request_path: Path, work: Path, store: Path) -> PreparationReceipt:
    """Run one fresh acquisition/fixture/preparation; retain every failed phase."""
    captured = regular_bytes(request_path)
    request = Acquisition.model_validate_json(captured)
    if request.work != work / "acquisition":
        raise ReleaseRejectedError("acquisition work must be the image evidence acquisition child")
    if work.resolve(strict=True) != work or store.parent.resolve(strict=True) != store.parent:
        raise ReleaseRejectedError("image evidence and release store must be canonical")
    store.mkdir(mode=0o700, exist_ok=True)
    if store.resolve(strict=True) != store:
        raise ReleaseRejectedError("release store must not be an alias")
    marker = work / "input-build.json"
    _write(marker, {"request": str(request_path), "digest": hashlib.sha256(captured).hexdigest()})
    phase = "acquisition"
    try:
        with _phase(work, phase):
            acquired = acquire_inputs(request)
            _verify_acquired(acquired)
        phase = "scripted-fixture"
        with _phase(work, phase):
            fixture = build_fixture(
                acquired.archive.path,
                acquired.source,
                work / "fixture",
                uv=acquired.inputs.uv.path,
                python=(acquired.inputs.python.root / "bin/python3").resolve(strict=True),
                cache_dir=acquired.inputs.cache_dir,
                build_constraints=acquired.inputs.build_constraints,
            )
        phase = "derive-proof-inputs"
        with _phase(work, phase):
            supplied = derive_inputs(acquired, fixture, work / "proof-inputs")
        phase = "prepare-image"
        with _phase(work, phase):
            receipt = prepare_image(
                Preparation(
                    repo=request.repo,
                    commit=request.commit,
                    work=work / "preparation",
                    store=store,
                    inputs=supplied,
                )
            )
        phase = "verify-retained-inputs"
        with _phase(work, phase):
            _verify_prepared(acquired, fixture, receipt, request_path, captured)
        _write(
            work / "prepared.json",
            {
                "request_digest": hashlib.sha256(captured).hexdigest(),
                "acquisition": str(request.work / "acquisition-receipt.json"),
                "fixture": str(work / "fixture/fixture-receipt.json"),
                "derivation": str(work / "proof-inputs/derivation.json"),
                "preparation": str(work / "preparation/receipt.json"),
                "preparation_digest": hashlib.sha256(encode(receipt)).hexdigest(),
            },
        )
        return receipt
    except BaseException as exc:
        try:
            _write(work / "failed.json", {"phase": phase, "error": repr(exc)})
        except OSError as recording:
            exc.add_note(f"could not retain image-input failure: {recording}")
        raise


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--request", type=Path, required=True)
    parser.add_argument("--work", type=Path, required=True)
    parser.add_argument("--store", type=Path, required=True)
    args = parser.parse_args()
    prepare_inputs(args.request, args.work, args.store)


if __name__ == "__main__":
    main()
