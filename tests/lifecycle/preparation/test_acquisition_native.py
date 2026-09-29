"""Opt-in small cold acquisition; downloads tools but never starts Ava or data services."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from cli.release_build import build_application
from cli.release_prepare.acquire import acquire_inputs, verify_acquisition
from cli.release_prepare.acquisition_dependencies import file_input, tree_input
from cli.release_prepare.acquisition_models import Acquisition, AcquisitionReceipt
from cli.release_prepare.source_distributions import export_distributions
from shared.runtime_release import ReleaseRejectedError

# ruff: noqa: S603 -- explicitly opted-in cold tools operate only within tmp_path.


@pytest.mark.skipif(
    os.environ.get("AVA_NATIVE_RELEASE_ACQUIRE") != "1",
    reason="explicit cold-network acquisition proof",
)
def test_cold_acquisition_builds_locked_sdist_with_pinned_tools(tmp_path: Path) -> None:
    root = tmp_path.resolve()
    uv_path = shutil.which("uv")
    assert uv_path is not None
    uv = Path(uv_path).resolve(strict=True)
    repo = root / "repo"
    repo.mkdir()
    for name in ("shared", "db", "migrations"):
        (repo / name).mkdir()
    (repo / "shared/__init__.py").write_text("")
    (repo / "db/schema.sql").write_text("SELECT 1;\n")
    (repo / "migrations/.gitkeep").write_text("")
    (repo / ".python-version").write_text("3.12.12\n")
    (repo / "pyproject.toml").write_text(
        '[project]\nname="ava"\nversion="0.1.5"\nrequires-python=">=3.12"\n'
        'dependencies=["crcmod==1.7", "packaging==26.3"]\n'
        '[build-system]\nrequires=["hatchling"]\nbuild-backend="hatchling.build"\n'
        '[tool.hatch.build.targets.wheel]\npackages=["shared"]\n'
        '[tool.hatch.build.targets.wheel.force-include]\n"db/schema.sql"="db/schema.sql"\n"migrations"="migrations"\n'
    )
    environment = {"PATH": os.defpath, "HOME": str(root), "UV_CACHE_DIR": str(root / "lock-cache")}
    subprocess.run(
        [str(uv), "--no-config", "lock", "--python", "3.12.12"],
        cwd=repo,
        env=environment,
        check=True,
        timeout=120,
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
    constraints = Path(__file__).resolve().parents[3] / "cli/release_prepare/build-tools.txt"
    result = acquire_inputs(
        Acquisition(
            repo=repo,
            commit=commit,
            work=root / "cold-acquisition",
            uv=file_input(uv),
            build_constraints=file_input(constraints),
        )
    )
    assert len(result.derivations) == 2
    assert {edge.kind for edge in result.derivations} == {"built-wheel", "downloaded-wheel"}
    assert result.derivations[0].package == "crcmod"
    assert result.inputs.python.root.is_relative_to(result.request.work / "python")
    assert result.inputs.wheelhouse.root.is_relative_to(result.request.work)
    application = build_application(
        repo,
        commit,
        root / "application",
        uv=uv,
        python=result.inputs.python.root / "bin/python3",
        cache_dir=result.inputs.cache_dir,
        build_constraints=result.inputs.build_constraints.path,
    )
    assert application.source_commit == commit and application.wheel.is_file()
    # uv's interpreter query and build backend both execute the admitted Python.
    # The original receipt must remain valid after that separate offline build.
    verify_acquisition(result)
    missing_backend = root / "missing-backend.txt"
    missing_backend.write_text(
        re.sub(
            r"^hatchling==[^\n]*(?:\n[ \t]+[^\n]*)*\n",
            "",
            constraints.read_text(),
            flags=re.MULTILINE,
        )
    )
    with pytest.raises(subprocess.CalledProcessError) as rejected:
        build_application(
            repo,
            commit,
            root / "unconstrained-backend",
            uv=uv,
            python=result.inputs.python.root / "bin/python3",
            cache_dir=result.inputs.cache_dir,
            build_constraints=missing_backend,
        )
    assert "hash" in rejected.value.stderr.lower()
    verify_acquisition(result)
    _prove_retained_seed(result, root, repo)


def _prove_retained_seed(result: AcquisitionReceipt, root: Path, repo: Path) -> None:
    store = root / "retained-inputs"
    store.mkdir(mode=0o700)
    seed = export_distributions(file_input(result.request.work / "acquisition-receipt.json"), store)
    shutil.rmtree(result.request.work)
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
            "--allow-empty",
            "-qm",
            "new application",
        ],
        check=True,
    )
    target = subprocess.check_output(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], text=True
    ).strip()
    reused = acquire_inputs(
        result.request.model_copy(
            update={
                "commit": target,
                "work": root / "seeded-acquisition",
                "source_distributions": seed,
            }
        )
    )
    assert reused.source.source_commit == target != result.source.source_commit
    assert {edge.kind for edge in reused.derivations} == {"built-wheel", "downloaded-wheel"}
    download = next(c for c in reused.commands if "--no-build-isolation" in c.argv)
    assert "--no-index" in download.argv and "--index-url" not in download.argv
    assert str(seed.root) in download.argv and download.returncode == 0
    verify_acquisition(reused)
    incomplete = root / "incomplete"
    incomplete.mkdir()
    wheel = next(seed.root.glob("*.whl"))
    shutil.copyfile(wheel, incomplete / wheel.name)
    missing = reused.request.model_copy(
        update={
            "work": root / "missing-acquisition",
            "source_distributions": tree_input(incomplete),
        }
    )
    with pytest.raises(ReleaseRejectedError, match="acquisition command failed"):
        acquire_inputs(missing)
    assert not (missing.work / "acquisition-receipt.json").exists()
