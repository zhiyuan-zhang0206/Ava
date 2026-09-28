"""The host's bare `ava`: `scripts/ava-launcher.sh` runs the CLI of the cluster
`$AVA_HOME` names, with no default and no fallback; `install.sh` points
`~/.local/bin/ava` at it for the prod install only."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_LAUNCHER = _REPO_ROOT / "scripts" / "ava-launcher.sh"


def _launch(env: dict[str, str], *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 — the repo's own launcher, fixed argv
        [str(_LAUNCHER), *args],
        env={"PATH": "/usr/bin:/bin", **env},
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


def test_launcher_runs_the_cli_its_home_links(tmp_path: Path) -> None:
    cli = tmp_path / "checkout" / ".venv" / "bin" / "ava"
    cli.parent.mkdir(parents=True)
    cli.write_text('#!/bin/sh\necho "cli: $*"\n')
    cli.chmod(0o755)
    home = tmp_path / "cluster-home"
    home.mkdir()
    (home / "ava").symlink_to(cli)

    proc = _launch({"AVA_HOME": str(home)}, "impersonate", "status", "4", "--agent", "7")

    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == "cli: impersonate status 4 --agent 7\n"


def test_launcher_refuses_without_ava_home() -> None:
    proc = _launch({}, "status")

    assert proc.returncode == 2
    assert "AVA_HOME is not set" in proc.stderr
    assert proc.stdout == ""


def test_launcher_refuses_a_home_without_its_cli_link(tmp_path: Path) -> None:
    proc = _launch({"AVA_HOME": str(tmp_path / "cluster-home")}, "status")

    assert proc.returncode == 2
    assert f"{tmp_path / 'cluster-home'}/ava does not exist" in proc.stderr


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


def test_prod_install_links_bare_ava_to_the_launcher(tmp_path: Path) -> None:
    proc, bare_link = _run_install(tmp_path, prod=True)

    assert proc.returncode == 0, proc.stderr
    launcher = bare_link.parents[2] / ".ava" / "source" / "scripts" / "ava-launcher.sh"
    assert bare_link.readlink() == launcher
    assert "WARNING" not in proc.stderr


def test_prod_install_repoints_a_checkout_link_to_the_launcher(tmp_path: Path) -> None:
    old_target = tmp_path / "home" / ".ava" / "source" / ".venv" / "bin" / "ava"
    proc, bare_link = _run_install(tmp_path, prod=True, existing_target=old_target)

    assert proc.returncode == 0, proc.stderr
    assert bare_link.readlink().name == "ava-launcher.sh"


def test_non_prod_install_leaves_the_host_link_alone(tmp_path: Path) -> None:
    prod_launcher = tmp_path / "prod" / "source" / "scripts" / "ava-launcher.sh"
    proc, bare_link = _run_install(tmp_path, prod=False, existing_target=prod_launcher)

    assert proc.returncode == 0, proc.stderr
    assert bare_link.readlink() == prod_launcher
    assert "WARNING" not in proc.stderr


def test_non_prod_install_without_link_notes_the_checkout_cli(tmp_path: Path) -> None:
    proc, bare_link = _run_install(tmp_path, prod=False)

    assert proc.returncode == 0, proc.stderr
    assert not bare_link.exists()
    assert "has no bare `ava`" in proc.stderr
    assert ".venv/bin/ava" in proc.stderr
