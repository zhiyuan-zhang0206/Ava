"""Real committed capture with controlled package tools; no network or service effects."""

from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

from cli.release_prepare import acquire, acquisition_process
from cli.release_prepare.acquisition_dependencies import file_input, tree_input
from cli.release_prepare.acquisition_models import Acquisition, AcquisitionReceipt, FrontendTools
from cli.release_prepare.acquisition_process import Commands
from cli.release_prepare.models import TreeInput
from shared.runtime_prepare import _python_input_inventory, inventory_digest
from shared.runtime_release import ReleaseRejectedError

# ruff: noqa: S603 -- real Git and isolated import checks use only generated fixture inputs.


def _wheel(name: str, version: str) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr(
            zipfile.ZipInfo(f"{name}-{version}.dist-info/METADATA", (2000, 1, 1, 0, 0, 0)),
            f"Name: {name}\nVersion: {version}\n",
        )
    return output.getvalue()


@pytest.fixture
def acquisition(tmp_path: Path) -> Acquisition:
    root = tmp_path.resolve()
    repo = root / "repo"
    repo.mkdir()
    for name in ("shared", "migrations", "db"):
        (repo / name).mkdir()
    (repo / "shared/__init__.py").write_text("")
    (repo / "migrations/.gitkeep").write_text("")
    (repo / "db/schema.sql").write_text("SELECT 1;\n")
    (repo / "pyproject.toml").write_text('[project]\nname="ava"\nversion="0.1.5"\n')
    (repo / ".python-version").write_text("3.12.12\n")
    wheel = _wheel("demo", "1.0")
    sdist = b"trusted fixture sdist, built only by the controlled tool"
    (repo / "uv.lock").write_text(
        f'[[package]]\nname="demo"\nversion="1.0"\nwheels=[{{hash="sha256:{hashlib.sha256(wheel).hexdigest()}"}}]\n'
        f'[[package]]\nname="other"\nversion="2.0"\nsdist={{hash="sha256:{hashlib.sha256(sdist).hexdigest()}"}}\n'
    )
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
    uv = root / "uv"
    uv.write_bytes(b"controlled tool; never executed")
    constraints = root / "build-tools.txt"
    constraints.write_text("hatchling==1.0 --hash=sha256:" + "a" * 64 + "\n")
    return Acquisition(
        repo=repo,
        commit=commit,
        work=root / "work",
        uv=file_input(uv),
        build_constraints=file_input(constraints),
    )


class PackageTools(Commands):
    corrupt_download = False
    change_export = False

    def run(
        self,
        argv: list[str],
        cwd: Path,
        *,
        timeout: int = 900,
        environment: dict[str, str] | None = None,
    ) -> str:
        assert "AVA_HOME" not in self.environment and "VIRTUAL_ENV" not in self.environment
        if argv[-1] == "--version":
            return "uv 0.10.2 (fixture)"
        assert "download" in argv and "--require-hashes" in argv
        assert "--no-build-isolation" in argv and "--no-deps" in argv
        target = Path(argv[argv.index("--dest") + 1])
        if "--find-links" in argv:
            assert "--no-index" in argv and "--index-url" not in argv
            seed = Path(argv[argv.index("--find-links") + 1])
            for name in ("demo-1.0-py3-none-any.whl", "other-2.0.tar.gz"):
                shutil.copyfile(seed / name, target / name)
            return ""
        (target / "demo-1.0-py3-none-any.whl").write_bytes(_wheel("demo", "1.0"))
        (target / "other-2.0.tar.gz").write_bytes(
            b"corrupt"
            if self.corrupt_download
            else b"trusted fixture sdist, built only by the controlled tool"
        )
        return ""

    def package(self, *argv: str, cwd: Path, timeout: int = 900) -> str:
        if argv[0] == "export":
            assert "--locked" in argv and "--no-emit-project" in argv and "--no-dev" in argv
            Path(argv[argv.index("--output-file") + 1]).write_text(
                f"demo==1.0 --hash=sha256:{hashlib.sha256(_wheel('demo', '1.0')).hexdigest()}\n"
                "other==2.0 --hash=sha256:"
                + hashlib.sha256(
                    b"trusted fixture sdist, built only by the controlled tool"
                ).hexdigest()
                + "\n"
            )
            if self.change_export:
                (cwd / "uv.lock").write_text("changed lock")
            return ""
        assert argv[:2] == ("--offline", "build")
        assert "--require-hashes" in argv and "--build-constraints" in argv
        output = Path(argv[argv.index("--out-dir") + 1])
        (output / "other-2.0-py3-none-any.whl").write_bytes(_wheel("other", "2.0"))
        return ""


@pytest.fixture
def tools(monkeypatch: pytest.MonkeyPatch) -> None:
    def python(commands: Commands, source: Path) -> tuple[TreeInput, Path, str]:
        assert (source / ".python-version").read_text() == "3.12.12\n"
        root = commands.work / "python/managed"
        (root / "bin").mkdir(parents=True)
        executable = root / "bin/python3"
        executable.write_bytes(b"managed interpreter fixture")
        return (
            TreeInput(root=root, digest=inventory_digest(_python_input_inventory(root))),
            executable,
            "3.12.12",
        )

    def environment(
        commands: Commands, python: Path, constraints: Path
    ) -> tuple[Path, str, TreeInput]:
        assert constraints.read_bytes()
        root = commands.work / "build-tools"
        root.mkdir()
        (root / "hatchling-fixture.whl").write_bytes(b"hash admitted tool fixture")
        return python, "pip bundled fixture", tree_input(root)

    monkeypatch.setattr(acquire, "Commands", PackageTools)
    monkeypatch.setattr(acquire, "managed_python", python)
    monkeypatch.setattr(acquire, "build_environment", environment)


def test_acquisition_binds_captured_lock_to_distinct_downloaded_and_built_wheels(
    acquisition: Acquisition,
    tools: None,
) -> None:
    (acquisition.repo / "uv.lock").write_text("dirty checkout must not supply dependencies")
    (acquisition.repo / ".python-version").write_text("3.99.0")
    receipt = acquire.acquire_inputs(acquisition)
    assert (
        AcquisitionReceipt.model_validate_json(
            (acquisition.work / "acquisition-receipt.json").read_bytes()
        )
        == receipt
    )
    downloaded, built = receipt.derivations
    assert (
        downloaded.kind == "downloaded-wheel"
        and downloaded.source.digest == downloaded.wheel.digest
    )
    assert built.kind == "built-wheel" and built.source.digest != built.wheel.digest
    assert built.source.path.name == "other-2.0.tar.gz"
    assert receipt.inputs.source_lock_digest == receipt.source_lock.digest
    assert receipt.inputs.requirements.path.read_text() == "".join(
        f"{item.package}=={item.package_version} --hash=sha256:{item.wheel.digest}\n"
        for item in receipt.derivations
    )
    assert (acquisition.repo / ".python-version").read_text() == "3.99.0"
    with pytest.raises(FileExistsError):
        acquire.acquire_inputs(acquisition)


@pytest.mark.parametrize("defect", ["corrupt_download", "change_export"])
def test_changed_acquisition_inputs_refuse_and_retain_evidence(
    acquisition: Acquisition,
    tools: None,
    monkeypatch: pytest.MonkeyPatch,
    defect: str,
) -> None:
    monkeypatch.setattr(PackageTools, defect, True)
    with pytest.raises(ReleaseRejectedError):
        acquire.acquire_inputs(acquisition)
    assert json.loads((acquisition.work / "failed.json").read_bytes())["phase"] == "dependencies"
    assert not (acquisition.work / "local-inputs.json").exists()
    assert (acquisition.work / "captured/source.tar").is_file()


def test_pinned_build_tools_are_verified_before_acquisition(acquisition: Acquisition) -> None:
    acquisition.build_constraints.path.write_text("floating hatchling")
    with pytest.raises(ReleaseRejectedError, match="tool bytes changed"):
        acquire.acquire_inputs(acquisition)
    assert not acquisition.work.exists()


def test_unknown_plugin_refuses_before_acquisition(acquisition: Acquisition) -> None:
    request = acquisition.model_copy(update={"required_plugins": ("missing",)})
    with pytest.raises(ReleaseRejectedError, match="plugin is missing"):
        acquire.acquire_inputs(request)
    assert not request.work.exists()


@pytest.mark.parametrize("owner", ["repo", "plugins", "npm"])
def test_work_cannot_be_directly_inside_a_supplied_input(
    acquisition: Acquisition, owner: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    def unexpected_capture(*_args: object) -> None:
        raise AssertionError("overlapping input reached source capture")

    monkeypatch.setattr(acquire, "capture_source", unexpected_capture)
    parent = acquisition.repo
    changes: dict[str, object] = {}
    if owner != "repo":
        parent = acquisition.repo.parent / owner
        parent.mkdir()
        if owner == "plugins":
            changes["plugins"] = tree_input(parent)
        else:
            changes["frontend"] = FrontendTools(
                node=acquisition.uv, npm=tree_input(parent), gateway_port=18203
            )
    changes["work"] = parent / "acquisition"
    request = acquisition.model_copy(update=changes)
    with pytest.raises(ReleaseRejectedError, match="overlaps input"):
        acquire.acquire_inputs(request)
    assert not request.work.exists()


def test_receipt_cannot_claim_another_committed_source(
    acquisition: Acquisition, tools: None
) -> None:
    receipt = acquire.acquire_inputs(acquisition)
    value = receipt.model_dump(mode="json")
    value["source"]["source_commit"] = "f" * 40
    with pytest.raises(ValueError, match="source and inputs disagree"):
        AcquisitionReceipt.model_validate_json(json.dumps(value))


def test_receipt_cannot_rehash_changed_source_inputs(acquisition: Acquisition, tools: None) -> None:
    receipt = acquire.acquire_inputs(acquisition)
    receipt.source_lock.path.write_text(receipt.source_lock.path.read_text() + "\n# changed\n")
    changed = file_input(receipt.source_lock.path)
    value = receipt.model_dump(mode="json")
    value["source_lock"] = changed.model_dump(mode="json")
    value["source_inputs"]["uv.lock"] = changed.model_dump(mode="json")
    value["inputs"]["source_lock_digest"] = changed.digest
    changed_receipt = AcquisitionReceipt.model_validate_json(json.dumps(value))
    with pytest.raises(ReleaseRejectedError, match="differs from the committed archive"):
        acquire.verify_acquisition(changed_receipt)


@pytest.mark.parametrize("defect", ["missing", "redirected"])
def test_receipt_lock_must_be_the_archived_lock(
    acquisition: Acquisition, tools: None, defect: str
) -> None:
    receipt = acquire.acquire_inputs(acquisition)
    value = receipt.model_dump(mode="json")
    if defect == "missing":
        del value["source_inputs"]["uv.lock"]
    else:
        value["source_lock"]["path"] = str(acquisition.work / "uncommitted-uv.lock")
    with pytest.raises(ValueError, match="source and inputs disagree"):
        AcquisitionReceipt.model_validate_json(json.dumps(value))


def test_missing_asset_output_is_not_a_verified_empty_tree(tmp_path: Path) -> None:
    with pytest.raises(ReleaseRejectedError, match="existing directories"):
        tree_input(tmp_path.resolve() / "missing")


def test_command_environment_excludes_ambient_runtime_and_package_authority(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    work = tmp_path.resolve()
    for name in (
        "AVA_HOME",
        "AVA_CLUSTER_SECRET",
        "PYTHONPATH",
        "UV_CONFIG_FILE",
        "PIP_INDEX_URL",
        "VIRTUAL_ENV",
    ):
        monkeypatch.setenv(name, "must-not-be-forwarded")
    commands = Commands(work, Path(sys.executable))
    result = commands.run(
        [sys.executable, "-I", "-B", "-c", "import json,os;print(json.dumps(dict(os.environ)))"],
        work,
    )
    observed = json.loads(result)
    for name in (
        "AVA_HOME",
        "AVA_CLUSTER_SECRET",
        "PYTHONPATH",
        "UV_CONFIG_FILE",
        "PIP_INDEX_URL",
        "VIRTUAL_ENV",
    ):
        assert name not in observed
    assert observed["HOME"] == str(work / "home")
    assert observed["UV_CACHE_DIR"] == str(work / "cache")
    assert observed["PIP_CONFIG_FILE"] == os.devnull
    assert commands.evidence[0].returncode == 0


def test_timeout_retains_partial_tool_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    work = tmp_path.resolve()
    commands = Commands(work, Path(sys.executable))

    def timeout(*_args: object, **_kwargs: object) -> None:
        raise subprocess.TimeoutExpired(
            ["fixture"], 1, output=b"partial stdout", stderr=b"partial stderr"
        )

    monkeypatch.setattr(acquisition_process, "run_owned_command", timeout)
    with pytest.raises(subprocess.TimeoutExpired):
        commands.run(["fixture"], work)
    (record,) = commands.evidence
    assert record.timed_out and record.returncode is None
    assert record.output.path.read_text() == "partial stdoutpartial stderr"
    progress = json.loads((work / "command-progress.json").read_text())
    assert progress["status"] == "timed-out" and "returncode" not in progress
    assert progress["elapsed_seconds"] >= record.elapsed_seconds >= 0


def test_acquisition_and_collector_imports_do_not_load_runtime_authority() -> None:
    code = """
import importlib.abc, sys
sys.path.insert(0, sys.argv[1])
class Guard(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, *args):
        if fullname.startswith(('shared.config', 'shared.db', 'cli.commands', 'services.')):
            raise AssertionError('runtime authority imported: ' + fullname)
sys.meta_path.insert(0, Guard())
import cli.release_prepare.acquire
import scripts.prepare_otel_release
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


def test_collector_preparation_imports_with_only_managed_python_stdlib() -> None:
    code = """
import sys
sys.path.insert(0, sys.argv[1])
import scripts.prepare_otel_release
assert 'psutil' not in sys.modules
assert 'shared.config' not in sys.modules
"""
    result = subprocess.run(
        [sys.executable, "-I", "-S", "-B", "-c", code, str(Path(__file__).resolve().parents[3])],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_live_command_progress_is_visible_before_child_work(tmp_path: Path) -> None:
    commands = Commands(tmp_path.resolve(), Path(sys.executable))
    code = (
        "import json,pathlib,time;"
        "print(pathlib.Path('command-progress.json').read_text());"
        "time.sleep(0.02)"
    )
    started = json.loads(commands.run([sys.executable, "-I", "-B", "-c", code], commands.work))
    assert started["status"] == "running" and started["index"] == 0
    finished = json.loads((commands.work / "command-progress.json").read_text())
    assert finished["status"] == "passed" and finished["returncode"] == 0
    (evidence,) = commands.evidence
    assert evidence.started_at.isoformat() == started["started_at"]
    assert evidence.elapsed_seconds >= 0.02
    assert finished["elapsed_seconds"] >= evidence.elapsed_seconds


def test_interrupted_command_retains_diagnostic_without_claiming_exit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    commands = Commands(tmp_path.resolve(), Path(sys.executable))

    def interrupted(*_args: object, **_kwargs: object) -> None:
        raise KeyboardInterrupt("cancelled fixture")

    monkeypatch.setattr(acquisition_process, "run_owned_command", interrupted)
    with pytest.raises(KeyboardInterrupt, match="cancelled fixture"):
        commands.run(["fixture"], commands.work)
    progress = json.loads((commands.work / "command-progress.json").read_text())
    assert progress["status"] == "interrupted-or-failed"
    assert progress["error_type"] == "KeyboardInterrupt" and "returncode" not in progress
    assert not commands.evidence


@pytest.mark.parametrize("kind", ["custody", "interrupt", "timeout"])
def test_terminal_progress_failure_preserves_primary_exception(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    primary = {
        "custody": RuntimeError("native custody remains unknown"),
        "interrupt": KeyboardInterrupt("operator interruption"),
        "timeout": subprocess.TimeoutExpired(["fixture"], 1),
    }[kind]
    commands = Commands(tmp_path.resolve(), Path(sys.executable))
    original_progress = commands._progress

    def progress(value: dict[str, object]) -> None:
        if value["status"] != "running":
            raise OSError("diagnostic disk failure")
        original_progress(value)

    def failed(*_args: object, **_kwargs: object) -> None:
        raise primary

    monkeypatch.setattr(commands, "_progress", progress)
    monkeypatch.setattr(acquisition_process, "run_owned_command", failed)
    with pytest.raises(type(primary)) as error:
        commands.run(["fixture"], commands.work)
    assert error.value is primary
    assert "diagnostic disk failure" in " ".join(primary.__notes__)
    assert json.loads((commands.work / "command-progress.json").read_text())["status"] == "running"
    if kind != "timeout":
        assert not commands.evidence


def test_timeout_record_failure_still_records_progress_and_preserves_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    primary = subprocess.TimeoutExpired(["fixture"], 1)
    commands = Commands(tmp_path.resolve(), Path(sys.executable))

    def failed(*_args: object, **_kwargs: object) -> None:
        raise primary

    def record_failed(*_args: object, **_kwargs: object) -> None:
        raise OSError("log disk failure")

    monkeypatch.setattr(acquisition_process, "run_owned_command", failed)
    monkeypatch.setattr(commands, "_record", record_failed)
    with pytest.raises(subprocess.TimeoutExpired) as error:
        commands.run(["fixture"], commands.work)
    assert error.value is primary and not commands.evidence
    assert "log disk failure" in " ".join(primary.__notes__)
    assert (
        json.loads((commands.work / "command-progress.json").read_text())["status"] == "timed-out"
    )


def test_initial_progress_failure_refuses_before_native_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    commands = Commands(tmp_path.resolve(), Path(sys.executable))

    def progress_failed(_value: dict[str, object]) -> None:
        raise OSError("no diagnostic storage")

    def unexpected(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("command launched without initial progress")

    monkeypatch.setattr(commands, "_progress", progress_failed)
    monkeypatch.setattr(acquisition_process, "run_owned_command", unexpected)
    with pytest.raises(OSError, match="no diagnostic storage"):
        commands.run(["fixture"], commands.work)


def test_acquisition_failure_retains_secondary_diagnostic_notes(
    acquisition: Acquisition, tools: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    primary = RuntimeError("native custody remains unknown")
    primary.add_note("could not retain acquisition command diagnostic: disk failure")

    def failed(*_args: object, **_kwargs: object) -> str:
        raise primary

    monkeypatch.setattr(PackageTools, "run", failed)
    with pytest.raises(RuntimeError) as error:
        acquire.acquire_inputs(acquisition)
    assert error.value is primary
    failure = json.loads((acquisition.work / "failed.json").read_bytes())
    assert failure["error_type"] == "RuntimeError" and failure["error"] == str(primary)
    assert failure["notes"] == primary.__notes__
