"""The uv version every install path uses comes from one canonical source.

``base.host.brew_pin`` (UV_VERSION / UV_ASSET_SHA256) is canonical. toolchain.sh
embeds the same values because it runs before Python exists on a fresh box;
every CI workflow pins setup-uv with the same version, and toolchain.sh's
already-present branch accepts only that exact version. These tests fail when
any of the copies drift.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path
from typing import Any, cast

import yaml

from base.host import brew_pin

_REPO_ROOT = Path(__file__).resolve().parents[2]
_TOOLCHAIN = _REPO_ROOT / "scripts" / "provision" / "toolchain.sh"
# Every workflow file: any setup-uv pin in any of them is in scope.
_CI_WORKFLOWS = tuple(sorted((_REPO_ROOT / ".github" / "workflows").glob("*.yml")))

# One `echo "<platform-tag> <sha256>" ;;` arm per supported platform.
_TAG_SHA_ARM = re.compile(r'^\s*echo "([a-z0-9_-]+) ([0-9a-f]{64})"\s*;;', re.MULTILINE)


def test_toolchain_embeds_the_canonical_version() -> None:
    text = _TOOLCHAIN.read_text(encoding="utf-8")
    match = re.search(r'^UV_VERSION="([0-9][0-9.]*)"', text, re.MULTILINE)
    assert match is not None, "toolchain.sh must define UV_VERSION"
    assert match.group(1) == brew_pin.UV_VERSION


def test_toolchain_sha256_map_matches_the_canonical_set() -> None:
    text = _TOOLCHAIN.read_text(encoding="utf-8")
    embedded = dict(_TAG_SHA_ARM.findall(text))
    assert embedded == brew_pin.UV_ASSET_SHA256


def _workflow_document(workflow: Path) -> dict[str, Any]:
    """Parse a workflow file; the YAML boundary stays explicit for the checker."""
    document = yaml.safe_load(workflow.read_text(encoding="utf-8"))
    assert isinstance(document, dict), f"{workflow.name} is not a YAML mapping"
    return cast("dict[str, Any]", document)


def _workflow_jobs(workflow: Path) -> dict[str, Any]:
    jobs = _workflow_document(workflow).get("jobs")
    assert isinstance(jobs, dict), f"{workflow.name}: no jobs mapping"
    return cast("dict[str, Any]", jobs)


def test_every_workflow_setup_uv_pins_the_canonical_version() -> None:
    checked = 0
    for workflow in _CI_WORKFLOWS:
        for job_name, job in _workflow_jobs(workflow).items():
            steps = job.get("steps")
            if steps is None:
                continue
            for step in steps:
                if "astral-sh/setup-uv" not in str(step.get("uses", "")):
                    continue
                checked += 1
                with_ = step.get("with")
                pinned = with_.get("version") if with_ is not None else None
                assert pinned is not None, (
                    f"{workflow.name}: {job_name}: {step.get('name', 'unnamed')!r} uses "
                    f"setup-uv without a `version:` input — expected {brew_pin.UV_VERSION!r}"
                )
                assert str(pinned) == brew_pin.UV_VERSION, (
                    f"{workflow.name}: {job_name}: pins setup-uv version {pinned!r}, "
                    f"expected {brew_pin.UV_VERSION!r}"
                )
    assert checked > 0, "no setup-uv steps found — the workflow glob or the pin moved"


def _fake_uv(tmp_path: Path, version_output: str) -> Path:
    """A bin/ directory whose `uv` answers `--version` with `version_output`."""
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_uv = fake_bin / "uv"
    fake_uv.write_text(f"#!/bin/sh\necho '{version_output}'\n", encoding="utf-8")
    fake_uv.chmod(0o755)
    return fake_bin


def _run_toolchain_with_uv(fake_bin: Path, home: Path) -> subprocess.CompletedProcess[str]:
    """Run the real toolchain.sh with the fake uv first on the PATH."""
    env = {"PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}", "HOME": str(home)}
    return subprocess.run(  # noqa: S603 — the repository script under a purpose-built PATH
        ["bash", str(_TOOLCHAIN)], env=env, capture_output=True, text=True, check=False
    )


def test_toolchain_refuses_a_uv_of_the_wrong_version(tmp_path: Path) -> None:
    done = _run_toolchain_with_uv(_fake_uv(tmp_path, "uv 9.9.9 (imposter)"), tmp_path)
    assert done.returncode == 2, done.stdout + done.stderr
    assert "9.9.9" in done.stderr
    assert brew_pin.UV_VERSION in done.stderr


def test_toolchain_accepts_a_uv_of_the_pinned_version(tmp_path: Path) -> None:
    done = _run_toolchain_with_uv(_fake_uv(tmp_path, f"uv {brew_pin.UV_VERSION} (fake)"), tmp_path)
    assert done.returncode == 0, done.stdout + done.stderr
    assert "already present" in done.stdout


def test_toolchain_script_parses() -> None:
    subprocess.run(  # noqa: S603 — fixed argv executes the repository script
        ["bash", "-n", str(_TOOLCHAIN)], check=True
    )
