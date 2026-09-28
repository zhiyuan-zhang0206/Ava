"""Reusable raw inputs preserve fresh source and private acquisition authority."""

from __future__ import annotations

import json
import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from cli.release_prepare import source_distributions
from cli.release_prepare.acquire import acquire_inputs, verify_acquisition
from cli.release_prepare.acquisition_dependencies import (
    _distribution_source,
    file_input,
    tree_input,
)
from cli.release_prepare.acquisition_models import Acquisition
from cli.release_prepare.source_distributions import export_distributions, validate_distributions
from shared.runtime_release import ReleaseRejectedError
from tests.lifecycle.preparation.test_acquisition import PackageTools
from tests.lifecycle.preparation.test_acquisition import acquisition as acquisition
from tests.lifecycle.preparation.test_acquisition import tools as tools

# ruff: noqa: S603 -- Git reads and writes only the generated fixture repository.


def _store(root: Path) -> Path:
    store = root / "inputs"
    store.mkdir(mode=0o700)
    return store


def test_export_survives_producer_deletion_and_two_fresh_consumers(
    acquisition: Acquisition, tools: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = acquire_inputs(acquisition)
    captured = file_input(acquisition.work / "acquisition-receipt.json")
    store = _store(acquisition.work.parent)
    seed = export_distributions(captured, store)
    assert export_distributions(captured, store) == seed
    assert seed.digest == original.source_distributions.digest
    before = {p.name: p.read_bytes() for p in seed.root.iterdir()}
    shutil.rmtree(acquisition.work)
    original_download = PackageTools.run

    def local_only(
        self: PackageTools,
        argv: list[str],
        cwd: Path,
        *,
        timeout: int = 900,
        environment: dict[str, str] | None = None,
    ) -> str:
        if "download" in argv:
            assert "--no-index" in argv and "--index-url" not in argv
        return original_download(self, argv, cwd, timeout=timeout, environment=environment)

    monkeypatch.setattr(PackageTools, "run", local_only)
    # A different commit with the same lock receives a new source identity.
    subprocess.run(
        [
            "git",
            "-C",
            str(acquisition.repo),
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.invalid",
            "-c",
            "core.hooksPath=/dev/null",
            "commit",
            "--allow-empty",
            "-qm",
            "new application",
        ],
        check=True,
    )
    commit = subprocess.check_output(
        ["git", "-C", str(acquisition.repo), "rev-parse", "HEAD"], text=True
    ).strip()

    def consume(name: str):
        return acquire_inputs(
            acquisition.model_copy(
                update={
                    "commit": commit,
                    "work": store.parent / name,
                    "source_distributions": seed,
                }
            )
        )

    with ThreadPoolExecutor(max_workers=2) as workers:
        first, second = list(workers.map(consume, ("first", "second")))
    assert first.source.source_commit == second.source.source_commit == commit
    assert first.source != original.source
    assert first.inputs.cache_dir != second.inputs.cache_dir
    assert first.inputs.wheelhouse.root != second.inputs.wheelhouse.root
    for receipt in (first, second):
        assert receipt.request.source_distributions == seed
        assert {edge.kind for edge in receipt.derivations} == {"downloaded-wheel", "built-wheel"}
        verify_acquisition(receipt)
    assert before == {p.name: p.read_bytes() for p in seed.root.iterdir()}
    assert not acquisition.work.exists()


@pytest.mark.parametrize("defect", ["digest", "mutate", "extra", "symlink", "overlap"])
def test_seed_refuses_before_acquisition_effects(
    acquisition: Acquisition, tools: None, defect: str
) -> None:
    source = acquisition.work.parent / "seed"
    source.mkdir()
    artifact = source / "demo.whl"
    artifact.write_bytes(b"package")
    seed = tree_input(source)
    if defect == "digest":
        seed = seed.model_copy(update={"digest": "f" * 64})
    elif defect == "mutate":
        artifact.write_bytes(b"mutated")
    elif defect == "extra":
        (source / "extra").write_bytes(b"extra")
    elif defect == "symlink":
        (source / "alias").symlink_to(artifact)
    work = source / "work" if defect == "overlap" else acquisition.work
    with pytest.raises(ReleaseRejectedError):
        acquire_inputs(acquisition.model_copy(update={"work": work, "source_distributions": seed}))
    assert not work.exists()


@pytest.mark.parametrize(
    "requirements",
    [
        "demo @ https://invalid.example/demo.whl --hash=sha256:{hash}",
        "demo @ file:///uncontrolled/demo.whl --hash=sha256:{hash}",
        "--requirement /uncontrolled/requirements.txt",
        "demo==1.0 --hash=sha256:{hash} --extra-index-url https://invalid.example",
        "demo>=1.0 --hash=sha256:{hash}",
        "demo==1.* --hash=sha256:{hash}",
    ],
)
def test_seed_never_allows_nonlocal_or_unpinned_requirements(
    tmp_path: Path, requirements: str
) -> None:
    seed_root = tmp_path / "seed"
    seed_root.mkdir()
    (seed_root / "demo.whl").write_bytes(b"package")
    exported = tmp_path / "requirements.txt"
    exported.write_text(requirements.format(hash="a" * 64))
    with pytest.raises(ReleaseRejectedError):
        _distribution_source(tree_input(seed_root), exported)


def test_seed_accepts_hash_continuations_and_environment_markers(tmp_path: Path) -> None:
    seed_root = tmp_path / "seed"
    seed_root.mkdir()
    (seed_root / "demo.whl").write_bytes(b"package")
    exported = tmp_path / "requirements.txt"
    exported.write_text(
        '# locked export\ndemo==1.0 ; python_version >= "3.12" \\\n'
        "    --hash=sha256:" + "a" * 64 + " \\\n    --hash=sha256:" + "b" * 64 + "\n"
    )
    assert _distribution_source(tree_input(seed_root), exported) == [
        "--no-index",
        "--find-links",
        str(seed_root),
    ]


def test_export_requires_captured_receipt_bytes(acquisition: Acquisition, tools: None) -> None:
    acquire_inputs(acquisition)
    captured = file_input(acquisition.work / "acquisition-receipt.json")
    captured.path.write_bytes(captured.path.read_bytes() + b"\n")
    store = _store(acquisition.work.parent)
    with pytest.raises(ReleaseRejectedError, match="receipt bytes changed"):
        export_distributions(captured, store)
    assert not list(store.iterdir())


def test_export_does_not_reencode_captured_optional_fields(
    acquisition: Acquisition, tools: None
) -> None:
    acquire_inputs(acquisition)
    receipt = acquisition.work / "acquisition-receipt.json"
    value = json.loads(receipt.read_bytes())
    del value["request"]["source_distributions"]
    receipt.write_text(json.dumps(value))
    request = acquisition.work / "request.json"
    value = json.loads(request.read_bytes())
    del value["source_distributions"]
    request.write_text(json.dumps(value))
    seed = export_distributions(file_input(receipt), _store(acquisition.work.parent))
    validate_distributions(seed)


@pytest.mark.parametrize("defect", ["copy", "interruption", "existing"])
def test_export_never_publishes_partial_or_overwrites_invalid_tree(
    acquisition: Acquisition, tools: None, monkeypatch: pytest.MonkeyPatch, defect: str
) -> None:
    receipt = acquire_inputs(acquisition)
    store = _store(acquisition.work.parent)
    target = store / receipt.source_distributions.digest
    if defect == "existing":
        target.mkdir()
    else:
        original = source_distributions.shutil.copyfile

        def broken(source: Path, destination: Path):
            if defect == "interruption":
                raise KeyboardInterrupt("controlled interruption")
            result = original(source, destination)
            destination.write_bytes(b"changed during copy")
            return result

        monkeypatch.setattr(source_distributions.shutil, "copyfile", broken)
    expected = KeyboardInterrupt if defect == "interruption" else ReleaseRejectedError
    with pytest.raises(expected):
        export_distributions(file_input(acquisition.work / "acquisition-receipt.json"), store)
    assert target.exists() == (defect == "existing")
    assert not list(store.glob(".capture-*"))


def test_seed_mutation_after_use_invalidates_receipt(acquisition: Acquisition, tools: None) -> None:
    receipt = acquire_inputs(acquisition)
    seed = export_distributions(
        file_input(acquisition.work / "acquisition-receipt.json"), _store(acquisition.work.parent)
    )
    reused = acquire_inputs(
        acquisition.model_copy(
            update={
                "work": acquisition.work.parent / "reused",
                "source_distributions": seed,
            }
        )
    )
    artifact = next(seed.root.iterdir())
    artifact.chmod(0o600)
    artifact.write_bytes(b"changed")
    with pytest.raises(ReleaseRejectedError, match="inventory changed"):
        verify_acquisition(reused)
    assert receipt.request.source_distributions is None
