"""Real committed-source build and inventory checks around a stub native assembler."""

from __future__ import annotations

import json
import platform
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

from cli.release_build import ApplicationBuild
from cli.release_prepare import FileInput, LocalInputs, Preparation, PreparationReceipt, TreeInput
from cli.release_prepare import prepare as preparation
from shared.runtime_prepare import PrepareInputs, inventory_digest, tree_inventory
from shared.runtime_release import (
    ReleaseRejectedError,
    VerifiedRelease,
    file_sha256,
    verify_release,
)

# ruff: noqa: S603 -- commands operate only on a generated fixture repository or isolated test child.


def _tree(path: Path) -> TreeInput:
    return TreeInput(root=path, digest=inventory_digest(tree_inventory(path)))


def _file(path: Path) -> FileInput:
    return FileInput(path=path, digest=file_sha256(path))


@pytest.fixture
def request_fixture(tmp_path: Path) -> Preparation:
    root = tmp_path.resolve()
    repo = root / "repo"
    repo.mkdir()
    for name in ("shared", "db", "migrations"):
        (repo / name).mkdir()
    (repo / "shared/__init__.py").write_text('VALUE = "committed"\n')
    (repo / "db/schema.sql").write_text("SELECT 1;\n")
    (repo / "uv.lock").write_text("version = 1\n")
    (repo / "migrations/.gitkeep").write_text("")
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.invalid",
            "-c",
            "core.hooksPath=/dev/null",
            "commit",
            "-qm",
            "fixture",
        ],
        check=True,
    )
    commit = subprocess.check_output(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], text=True
    ).strip()
    python = root / "python"
    (python / "bin").mkdir(parents=True)
    (python / "bin/python3").write_bytes(b"inert managed interpreter fixture")
    wheels = root / "wheels"
    wheels.mkdir()
    dependency = wheels / "example_dep-1.0-py3-none-any.whl"
    with zipfile.ZipFile(dependency, "w") as wheel:
        wheel.writestr("example_dep-1.0.dist-info/METADATA", "Name: example-dep\nVersion: 1.0\n")
    requirements = root / "requirements.txt"
    requirements.write_text(f"example-dep==1.0 --hash=sha256:{file_sha256(dependency)}\n")
    uv = root / "uv"
    uv.write_text(
        f"#!{sys.executable}\n"
        "import pathlib,sys,zipfile\n"
        "assert '--offline' in sys.argv and '--no-sources' in sys.argv\n"
        "assert '--cache-dir' in sys.argv\n"
        "out=pathlib.Path(sys.argv[sys.argv.index('--out-dir')+1]); out.mkdir()\n"
        "with zipfile.ZipFile(out/'ava-0.1.5-py3-none-any.whl','w') as z:\n"
        " for p in pathlib.Path('.').rglob('*'):\n"
        "  if p.is_file(): z.write(p,p.as_posix())\n"
    )
    uv.chmod(0o700)
    constraints = root / "build-tools.txt"
    constraints.write_text("hatchling==1.0 --hash=sha256:" + "a" * 64 + "\n")
    cache = root / "cache"
    cache.mkdir()
    store = root / "home/releases"
    store.mkdir(parents=True, mode=0o700)
    (store / "current-release").write_text("unreadable selector sentinel\n")
    (store.parent / ".env").write_text("unusable settings sentinel\n")
    (root / "clusters.json").write_text('{"untouched":true}\n')
    return Preparation(
        repo=repo,
        commit=commit,
        work=root / "work",
        store=store,
        inputs=LocalInputs(
            python=_tree(python),
            wheelhouse=_tree(wheels),
            requirements=_file(requirements),
            source_lock_digest=file_sha256(repo / "uv.lock"),
            uv=_file(uv),
            build_constraints=_file(constraints),
            cache_dir=cache,
        ),
    )


def _assemble(
    store: Path, inputs: PrepareInputs, *, changed_identity: bool = False
) -> VerifiedRelease:
    """Only native Python/pip assembly is substituted; the resulting inventory is real."""
    digest = inputs.wheelhouse_digest
    root = store / digest
    package = root / "venv/lib/python3.12/site-packages"
    package.mkdir(parents=True)
    with zipfile.ZipFile(inputs.wheelhouse / inputs.application_wheel) as archive:
        archive.extractall(package)
    interpreter = root / "venv/bin/python"
    interpreter.parent.mkdir()
    interpreter.write_bytes(b"inert retained interpreter fixture")
    if changed_identity:
        receipt = package / "shared/release-build.json"
        value = json.loads(receipt.read_bytes())
        value["source_tree"] = "f" * 40
        receipt.write_text(json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n")
    manifest = {
        "version": 1,
        "artifact_digest": digest,
        "platform": platform.platform(),
        "schema_digest": inputs.schema_digest,
        "interpreter": "venv/bin/python",
        "cwd": "venv",
        "files": tree_inventory(root),
    }
    (root / "manifest.json").write_text(json.dumps(manifest))
    return verify_release(
        store,
        digest,
        manifest_digest=file_sha256(root / "manifest.json"),
        platform_tag=platform.platform(),
        schema_digest=inputs.schema_digest,
    )


def _protected(request: Preparation) -> dict[Path, bytes]:
    paths = [
        request.store / "current-release",
        request.store.parent / ".env",
        request.work.parent / "clusters.json",
    ]
    paths.extend(
        p
        for root in (request.repo, request.inputs.wheelhouse.root)
        for p in root.rglob("*")
        if p.is_file()
    )
    return {path: path.read_bytes() for path in paths}


def test_committed_build_receipt_binds_real_installed_image_without_external_effects(
    request_fixture: Preparation,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = request_fixture
    (request.repo / "shared/__init__.py").write_text('VALUE = "dirty"\n')
    (request.repo / "uv.lock").write_text("dirty working lock must not be used\n")
    before = _protected(request)
    monkeypatch.setattr(preparation, "prepare_release", _assemble)
    receipt = preparation.prepare_image(request)
    stored = PreparationReceipt.model_validate_json((request.work / "receipt.json").read_bytes())
    assert stored == receipt
    assert stored.request_digest == file_sha256(request.work / "request.json")
    assert stored.source.source_commit == request.commit
    assert stored.build.source_lock_digest == request.inputs.source_lock_digest
    assert stored.build.wheel_digest == file_sha256(
        request.work / "application/wheels" / stored.build.wheel
    )
    member = stored.image.root / "venv/lib/python3.12/site-packages/shared/__init__.py"
    assert member.read_text() == 'VALUE = "committed"\n'
    assert _protected(request) == before
    assert not (request.work / "failed.json").exists()
    with pytest.raises(FileExistsError):
        preparation.prepare_image(request)


def test_offline_backend_imports_preserve_supplied_python_inventory(
    request_fixture: Preparation, monkeypatch: pytest.MonkeyPatch
) -> None:
    supplied = request_fixture.inputs
    (supplied.python.root / "build_probe.py").write_text("VALUE = 42\n")
    child = (
        f"import sys; sys.path.insert(0, {str(supplied.python.root)!r}); "
        "import build_probe; assert build_probe.VALUE == 42"
    )
    supplied.uv.path.write_text(
        supplied.uv.path.read_text()
        + f"import subprocess\nsubprocess.run([sys.executable, '-c', {child!r}], check=True)\n"
    )
    request = request_fixture.model_copy(
        update={
            "inputs": supplied.model_copy(
                update={"python": _tree(supplied.python.root), "uv": _file(supplied.uv.path)}
            )
        }
    )
    before = tree_inventory(request.inputs.python.root)
    monkeypatch.setattr(preparation, "prepare_release", _assemble)
    preparation.prepare_image(request)
    assert tree_inventory(request.inputs.python.root) == before
    assert not (request.inputs.python.root / "__pycache__").exists()


@pytest.mark.parametrize("member", ["requirements", "python", "wheelhouse", "uv"])
def test_tampered_supplied_inputs_fail_before_build_and_keep_evidence(
    request_fixture: Preparation,
    monkeypatch: pytest.MonkeyPatch,
    member: str,
) -> None:
    request = request_fixture
    target = {
        "requirements": request.inputs.requirements.path,
        "python": request.inputs.python.root / "bin/python3",
        "wheelhouse": next(request.inputs.wheelhouse.root.iterdir()),
        "uv": request.inputs.uv.path,
    }[member]
    target.write_bytes(target.read_bytes() + b"tampered")

    def forbidden(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("unverified input reached a build")

    monkeypatch.setattr(preparation, "build_application", forbidden)
    before = _protected(request)
    with pytest.raises(ReleaseRejectedError, match="changed"):
        preparation.prepare_image(request)
    assert json.loads((request.work / "failed.json").read_bytes())["phase"] == "validate-inputs"
    assert not (request.work / "receipt.json").exists()
    assert _protected(request) == before
    with pytest.raises(FileExistsError):
        preparation.prepare_image(request)


def test_supplied_lock_must_match_committed_archive(request_fixture: Preparation) -> None:
    request = request_fixture.model_copy(
        update={
            "inputs": request_fixture.inputs.model_copy(update={"source_lock_digest": "f" * 64})
        }
    )
    with pytest.raises(ReleaseRejectedError, match="lock digest"):
        preparation.prepare_image(request)
    assert (request.work / "application/build-receipt.json").is_file()
    assert not (request.work / "receipt.json").exists()
    assert list(request.store.iterdir()) == [request.store / "current-release"]


def test_fully_hashed_wrong_installed_source_receipt_is_rejected(
    request_fixture: Preparation,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def changed(store: Path, inputs: PrepareInputs) -> VerifiedRelease:
        return _assemble(store, inputs, changed_identity=True)

    monkeypatch.setattr(preparation, "prepare_release", changed)
    with pytest.raises(ReleaseRejectedError, match="installed application differs"):
        preparation.prepare_image(request_fixture)
    assert not (request_fixture.work / "receipt.json").exists()
    assert (
        json.loads((request_fixture.work / "failed.json").read_bytes())["phase"] == "verify-image"
    )
    assert len(list(request_fixture.store.iterdir())) == 2  # Failed image remains evidence.


def test_tampered_build_receipt_cannot_rebind_the_committed_wheel(
    request_fixture: Preparation,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = preparation.build_application

    def changed(
        repo: Path,
        commit: str,
        destination: Path,
        *,
        uv: Path,
        python: Path,
        build_constraints: Path,
        cache_dir: Path | None = None,
    ) -> ApplicationBuild:
        build = original(
            repo,
            commit,
            destination,
            uv=uv,
            python=python,
            cache_dir=cache_dir,
            build_constraints=build_constraints,
        )
        path = request_fixture.work / "application/build-receipt.json"
        value = json.loads(path.read_bytes())
        value["source_tree"] = "f" * 40
        path.write_text(json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n")
        return build

    def forbidden(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("changed source receipt reached native assembly")

    monkeypatch.setattr(preparation, "build_application", changed)
    monkeypatch.setattr(preparation, "prepare_release", forbidden)
    with pytest.raises(ReleaseRejectedError, match="embedded source identity"):
        preparation.prepare_image(request_fixture)
    assert not (request_fixture.work / "receipt.json").exists()


def test_private_copy_must_match_supplied_wheelhouse_not_a_changed_source(
    request_fixture: Preparation,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from cli.release_prepare import inputs as input_adapter

    original = input_adapter.shutil.copyfile
    source = next(request_fixture.inputs.wheelhouse.root.iterdir())

    def changed(src: Path, dst: Path) -> str | Path:
        result = original(src, dst)
        if src == source:
            dst.write_bytes(dst.read_bytes() + b"changed private copy")
        return result

    monkeypatch.setattr(input_adapter.shutil, "copyfile", changed)
    monkeypatch.setattr(preparation, "prepare_release", _assemble)
    with pytest.raises(ReleaseRejectedError, match="while privately copying"):
        preparation.prepare_image(request_fixture)
    assert not (request_fixture.work / "receipt.json").exists()


def test_missing_requested_plugin_is_rejected_before_build(
    request_fixture: Preparation,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plugins = request_fixture.work.parent / "plugins"
    plugins.mkdir()
    supplied = request_fixture.inputs.model_copy(
        update={"plugins": _tree(plugins), "required_plugins": ("required",)}
    )
    request = request_fixture.model_copy(update={"inputs": supplied})

    def forbidden(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("missing plugin reached the builder")

    monkeypatch.setattr(preparation, "build_application", forbidden)
    with pytest.raises(ReleaseRejectedError, match="requested plugin missing"):
        preparation.prepare_image(request)
    assert not (request.work / "application").exists()


def test_validated_input_document_cannot_omit_required_plugin_tree(
    request_fixture: Preparation,
) -> None:
    value = request_fixture.inputs.model_dump(mode="json") | {"required_plugins": ["required"]}
    with pytest.raises(ValueError, match="need a supplied plugin tree"):
        LocalInputs.model_validate_json(json.dumps(value))


def test_receipt_validation_rejects_rebinding_to_another_source(
    request_fixture: Preparation,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(preparation, "prepare_release", _assemble)
    receipt = preparation.prepare_image(request_fixture)
    value = receipt.model_dump(mode="json")
    value["source"]["source_commit"] = "f" * 40
    with pytest.raises(ValueError, match="source, inputs and image disagree"):
        PreparationReceipt.model_validate_json(json.dumps(value))


def test_controller_import_has_no_settings_or_legacy_lifecycle_dependency(
    request_fixture: Preparation,
) -> None:
    code = """
import builtins, sys
sys.path.insert(0, sys.argv[1])
original = builtins.__import__
def guarded(name, *args, **kwargs):
    if name == 'shared.config' or name.startswith('cli.commands') or name.startswith('cli.release_transition'):
        raise AssertionError('preparation imported runtime lifecycle: ' + name)
    return original(name, *args, **kwargs)
builtins.__import__ = guarded
from cli.release_prepare import Preparation
from cli.release_prepare.__main__ import main
assert 'shared.config' not in sys.modules
"""
    result = subprocess.run(
        [sys.executable, "-I", "-B", "-c", code, str(Path(__file__).resolve().parents[3])],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_controller_build_remains_settings_free_through_native_assembly_boundary(
    request_fixture: Preparation,
) -> None:
    request_file = request_fixture.work.parent / "preparation.json"
    request_file.write_text(request_fixture.model_dump_json())
    code = """
import importlib.abc, pathlib, sys
sys.path.insert(0, sys.argv[1])
class DenyRuntime(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, *args):
        if fullname.startswith(('shared.config', 'shared.db', 'cli.commands', 'cli.release_transition', 'services.')):
            raise AssertionError('preparation imported runtime authority: ' + fullname)
sys.meta_path.insert(0, DenyRuntime())
from cli.release_prepare import Preparation
from cli.release_prepare import prepare
class AssemblyBoundary(Exception):
    pass
def stop_at_assembly(*args):
    raise AssemblyBoundary()
prepare.prepare_release = stop_at_assembly
request = Preparation.model_validate_json(pathlib.Path(sys.argv[2]).read_bytes())
try:
    prepare.prepare_image(request)
except AssemblyBoundary:
    assert 'shared.config' not in sys.modules
else:
    raise AssertionError('controller did not reach native assembly')
"""
    before = _protected(request_fixture)
    result = subprocess.run(
        [
            sys.executable,
            "-I",
            "-B",
            "-c",
            code,
            str(Path(__file__).resolve().parents[3]),
            str(request_file),
        ],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert (request_fixture.work / "application/build-receipt.json").is_file()
    assert json.loads((request_fixture.work / "failed.json").read_bytes()) == {
        "phase": "prepare-runtime",
        "error_type": "AssemblyBoundary",
        "error": "",
    }
    assert _protected(request_fixture) == before


def test_build_cache_cannot_write_inside_supplied_python(
    request_fixture: Preparation,
) -> None:
    cache = request_fixture.inputs.python.root / "cache"
    cache.mkdir()
    request = request_fixture.model_copy(
        update={"inputs": request_fixture.inputs.model_copy(update={"cache_dir": cache})}
    )
    with pytest.raises(ReleaseRejectedError, match="build cache overlaps"):
        preparation.prepare_image(request)
    assert not request.work.exists()


def test_cli_emits_bound_receipt_and_refuses_work_reuse(
    request_fixture: Preparation,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from cli.release_prepare.__main__ import main

    request = request_fixture
    inputs = request.work.parent / "inputs.json"
    inputs.write_text(request.inputs.model_dump_json())
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "release_prepare",
            "--repo",
            str(request.repo),
            "--commit",
            request.commit,
            "--work",
            str(request.work),
            "--store",
            str(request.store),
            "--inputs",
            str(inputs),
        ],
    )
    monkeypatch.setattr(preparation, "prepare_release", _assemble)
    before = _protected(request)
    assert main() == 0
    output = capsys.readouterr()
    assert output.err == ""
    assert output.out.encode() == (request.work / "receipt.json").read_bytes()
    assert PreparationReceipt.model_validate_json(output.out).request == request
    assert main() == 1
    assert "release preparation failed" in capsys.readouterr().err
    assert _protected(request) == before
