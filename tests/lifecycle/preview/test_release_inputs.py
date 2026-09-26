"""Proof derivation preserves production provenance across each real file boundary."""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from cli.release_prepare import Preparation, PreparationReceipt
from cli.release_prepare.acquire import acquire_inputs
from cli.release_prepare.acquisition_models import Acquisition, AcquisitionReceipt
from cli.release_prepare.models import BuildEvidence, ImageEvidence, encode
from scripts.preview import release_inputs
from scripts.preview.release_fixture import FixtureWheel
from shared.runtime_release import ReleaseRejectedError, file_sha256
from tests.lifecycle.preparation.test_acquisition import acquisition as acquisition
from tests.lifecycle.preparation.test_acquisition import tools as tools


@pytest.fixture
def acquired(acquisition: Acquisition, tools: None) -> AcquisitionReceipt:
    return acquire_inputs(acquisition)


def _fixture(root: Path) -> FixtureWheel:
    wheel = root / "ava_preview_fixture-0.0.0-py3-none-any.whl"
    wheel.write_bytes(b"fixture build output; builder is covered separately")
    return FixtureWheel(wheel, file_sha256(wheel))


def test_derivation_keeps_original_production_receipt_and_inputs(
    acquired: AcquisitionReceipt, tmp_path: Path
) -> None:
    before = {p: p.read_bytes() for p in acquired.request.work.rglob("*") if p.is_file()}
    fixture = _fixture(tmp_path)
    supplied = release_inputs.derive_inputs(acquired, fixture, tmp_path / "derived")
    assert all(path.read_bytes() == original for path, original in before.items())
    assert supplied.python == acquired.inputs.python
    assert supplied.source_lock_digest == acquired.inputs.source_lock_digest
    assert supplied.wheelhouse.root != acquired.inputs.wheelhouse.root
    assert (
        supplied.wheelhouse.root / fixture.wheel.name
    ).read_bytes() == fixture.wheel.read_bytes()
    assert supplied.requirements.path.read_bytes() == (
        acquired.inputs.requirements.path.read_bytes().rstrip(b"\n")
        + f"\nava-preview-fixture==0.0.0 --hash=sha256:{fixture.digest}\n".encode()
    )
    derivation = json.loads((tmp_path / "derived/derivation.json").read_text())
    assert derivation["fixture_resolved_from_source_lock"] is False
    assert derivation["acquisition_digest"] == hashlib.sha256(encode(acquired)).hexdigest()
    assert derivation["production_inputs"] != derivation["derived_inputs"]


@pytest.mark.parametrize("changed", ["python", "wheel", "requirements", "receipt", "fixture"])
def test_changed_input_cannot_be_rehashed_into_new_proof_authority(
    acquired: AcquisitionReceipt, tmp_path: Path, changed: str
) -> None:
    fixture = _fixture(tmp_path)
    target = {
        "python": acquired.inputs.python.root / "bin/python3",
        "wheel": next(acquired.inputs.wheelhouse.root.glob("*.whl")),
        "requirements": acquired.inputs.requirements.path,
        "receipt": acquired.request.work / "acquisition-receipt.json",
        "fixture": fixture.wheel,
    }[changed]
    target.write_bytes(target.read_bytes() + b"changed")
    with pytest.raises(ReleaseRejectedError):
        release_inputs.derive_inputs(acquired, fixture, tmp_path / "derived")
    assert not (tmp_path / "derived/derivation.json").exists()


def test_copy_race_refuses_before_publishing_derived_inputs(
    acquired: AcquisitionReceipt, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    copy = shutil.copytree

    def corrupt(source: Path, destination: Path) -> Path:
        result = copy(source, destination)
        next(result.glob("*.whl")).write_bytes(b"changed private copy")
        return result

    monkeypatch.setattr(release_inputs.shutil, "copytree", corrupt)
    with pytest.raises(ReleaseRejectedError, match="copied production wheels differ"):
        release_inputs.derive_inputs(acquired, _fixture(tmp_path), tmp_path / "derived")
    assert not (tmp_path / "derived/local-inputs.json").exists()


def _prepared(request: Preparation, acquired: AcquisitionReceipt) -> PreparationReceipt:
    root = request.store / ("e" * 64)
    receipt = PreparationReceipt(
        request=request,
        request_digest=hashlib.sha256(encode(request)).hexdigest(),
        source=acquired.source,
        build=BuildEvidence(
            wheel="ava-0.1.5-py3-none-any.whl",
            wheel_digest="a" * 64,
            receipt_digest="b" * 64,
            source_lock_digest=request.inputs.source_lock_digest,
            combined_wheelhouse_digest=request.inputs.wheelhouse.digest,
        ),
        image=ImageEvidence(
            artifact_digest="e" * 64,
            manifest_digest="f" * 64,
            schema_digest=acquired.source.schema_digest,
            platform=acquired.platform,
            root=root,
            interpreter=root / "venv/bin/python",
            cwd=root / "venv",
        ),
    )
    request.work.mkdir()
    (request.work / "receipt.json").write_bytes(encode(receipt))
    return receipt


@pytest.mark.parametrize("changed", [None, "after-fixture", "after-prepare", "request"])
def test_pipeline_rechecks_production_bytes_after_fixture_and_preparation(
    acquisition: Acquisition, tools: None, monkeypatch: pytest.MonkeyPatch, changed: str | None
) -> None:
    work = acquisition.work
    work.mkdir()
    request = acquisition.model_copy(update={"work": work / "acquisition"})
    request_path = work / "request.json"
    request_path.write_bytes(encode(request))
    reached: list[str] = []

    def fixture(*args: object, **kwargs: object) -> FixtureWheel:
        reached.append("fixture")
        if changed == "after-fixture":
            path = request.work / "python/managed/bin/python3"
            path.write_bytes(b"mutated acquired Python")
        return _fixture(work)

    def prepare(value: Preparation) -> PreparationReceipt:
        reached.append("prepare")
        acquired = AcquisitionReceipt.model_validate_json(
            (request.work / "acquisition-receipt.json").read_bytes()
        )
        receipt = _prepared(value, acquired)
        if changed == "after-prepare":
            acquired.inputs.requirements.path.write_text("mutated requirements")
        if changed == "request":
            request_path.write_bytes(encode(request) + b" ")
        return receipt

    monkeypatch.setattr(release_inputs, "build_fixture", fixture)
    monkeypatch.setattr(release_inputs, "prepare_image", prepare)
    if changed:
        with pytest.raises(ReleaseRejectedError):
            release_inputs.prepare_inputs(request_path, work, work / "releases")
        assert not (work / "prepared.json").exists()
        assert (work / "failed.json").exists()
        if changed == "after-fixture":
            assert reached == ["fixture"]
    else:
        receipt = release_inputs.prepare_inputs(request_path, work, work / "releases")
        assert reached == ["fixture", "prepare"]
        evidence = json.loads((work / "prepared.json").read_text())
        assert evidence["preparation_digest"] == hashlib.sha256(encode(receipt)).hexdigest()
        with pytest.raises(FileExistsError):
            release_inputs.prepare_inputs(request_path, work, work / "releases")


def test_composition_imports_never_load_runtime_settings(tmp_path: Path) -> None:
    probe = """
import importlib.abc, sys
class DenyRuntime(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, *args):
        if fullname in {'shared.config', 'shared.dotenv_boot', 'cli.main'}:
            raise AssertionError('runtime authority imported: ' + fullname)
sys.meta_path.insert(0, DenyRuntime())
import scripts.preview.release_proof, scripts.preview.release_inputs
"""
    result = subprocess.run(  # noqa: S603 -- fixed import-only proof, no native runtime.
        [sys.executable, "-c", probe],
        cwd=Path(__file__).resolve().parents[3],
        env={"HOME": str(tmp_path)},
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
