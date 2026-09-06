"""`install.sh` must preserve the machine's prod bare-ava symlink."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]


def _run_install(
    tmp_path: Path, *, prod: bool, existing_target: Path | None = None
) -> tuple[subprocess.CompletedProcess[str], Path]:
    """Run a scratch gateway install with all host provisioners stubbed."""
    home = tmp_path / "home"
    install_home = home / ".ava" if prod else home / ".ava-preview"
    checkout = install_home / "source"
    provision = checkout / "scripts" / "provision"
    provision.mkdir(parents=True)
    shutil.copy(_REPO_ROOT / "scripts" / "install.sh", checkout / "scripts" / "install.sh")

    for name in ("node.sh", "database.sh"):
        script = provision / name
        script.write_text("#!/bin/sh\nexit 0\n")
        script.chmod(0o755)
    cli_tools = checkout / "scripts" / "install-cli-tools.sh"
    cli_tools.write_text("#!/bin/sh\nexit 0\n")
    cli_tools.chmod(0o755)
    toolchain = provision / "toolchain.sh"
    toolchain.write_text("#!/bin/sh\nexit 0\n")
    toolchain.chmod(0o755)

    stub_bin = tmp_path / "stub-bin"
    stub_bin.mkdir()
    uv = stub_bin / "uv"
    uv.write_text("#!/bin/sh\nexit 0\n")
    uv.chmod(0o755)
    uname = stub_bin / "uname"
    uname.write_text("#!/bin/sh\necho Linux\n")
    uname.chmod(0o755)
    python = checkout / ".venv" / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.write_text("#!/bin/sh\nexit 0\n")
    python.chmod(0o755)

    bare_link = home / ".local" / "bin" / "ava"
    if existing_target is not None:
        bare_link.parent.mkdir(parents=True)
        bare_link.symlink_to(existing_target)

    proc = subprocess.run(
        ["bash", "scripts/install.sh", "--role", "gateway"],
        cwd=checkout,
        env={
            "AVA_HOME": str(install_home),
            "AVA_ALLOW_ROOT_INSTALL": "1",
            "HOME": str(home),
            "PATH": f"{stub_bin}:/usr/bin:/bin",
        },
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    return proc, bare_link


def test_non_prod_install_preserves_existing_prod_link_and_warns(tmp_path: Path) -> None:
    prod_target = tmp_path / "prod" / "source" / ".venv" / "bin" / "ava"
    proc, bare_link = _run_install(tmp_path, prod=False, existing_target=prod_target)

    assert proc.returncode == 0, proc.stderr
    assert bare_link.readlink() == prod_target
    assert "WARNING non-prod install left" in proc.stderr
    assert str(prod_target) in proc.stderr
    assert "Re-link prod with:" in proc.stderr


def test_non_prod_install_without_link_warns_without_creating_one(tmp_path: Path) -> None:
    proc, bare_link = _run_install(tmp_path, prod=False)

    assert proc.returncode == 0, proc.stderr
    assert not bare_link.exists()
    assert "WARNING non-prod install did not create" in proc.stderr
    assert "Re-link prod with:" in proc.stderr


def test_prod_install_creates_bare_link_to_checkout(tmp_path: Path) -> None:
    proc, bare_link = _run_install(tmp_path, prod=True)

    assert proc.returncode == 0, proc.stderr
    assert (
        bare_link.readlink() == bare_link.parents[2] / ".ava" / "source" / ".venv" / "bin" / "ava"
    )
    assert "WARNING" not in proc.stderr


def test_prod_install_leaves_correct_existing_link_silent(tmp_path: Path) -> None:
    checkout_ava = tmp_path / "home" / ".ava" / "source" / ".venv" / "bin" / "ava"
    proc, bare_link = _run_install(tmp_path, prod=True, existing_target=checkout_ava)

    assert proc.returncode == 0, proc.stderr
    assert bare_link.readlink() == checkout_ava
    assert "WARNING" not in proc.stderr
