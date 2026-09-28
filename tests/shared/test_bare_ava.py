"""The host's bare `ava`: `scripts/ava-launcher.sh` runs the CLI of the cluster
`$AVA_HOME` names, with no default and no fallback. A home's first `ava start`
links the home's own CLI (`$AVA_HOME/ava`) and, for the production home only,
points `~/.local/bin/ava` at the launcher; until that start has run, its hints
name the checkout's own CLI."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

import cli.commands._setup as _setup_commands
from cli.commands.converge import _steps
from cli.commands.converge import host as converge_host
from shared.paths import repo_root

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


# The converge steps through which a first start (`_prepare_cold_start`) wires the
# CLI, taken from CONVERGE_STEPS with their real host-global gating.
_CLI_LINK_STEPS = tuple(
    step
    for step in converge_host.CONVERGE_STEPS
    if step.name in {"$AVA_HOME/ava CLI link", "ava launcher on PATH"}
)


def test_home_cli_link_is_the_first_write_step_of_every_cluster() -> None:
    """Later steps read this home's checkout through prod_source_dir(), which falls
    back to `$AVA_HOME/ava` when `$AVA_HOME/source` is absent — so every cluster
    writes the link first, after only the warning-only ownership preflight."""
    names = [step.name for step in converge_host.CONVERGE_STEPS]
    assert names[:2] == ["$AVA_HOME ownership preflight", "$AVA_HOME/ava CLI link"]
    assert not converge_host.CONVERGE_STEPS[1].host_global


def test_cli_link_steps_skip_hosts_without_the_launcher_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Windows has no launcher and no `$AVA_HOME/ava`: both link steps write nothing."""

    class _NoLauncher:
        def supports_ava_symlink(self) -> bool:
            return False

    monkeypatch.setattr(_steps, "get_backend", _NoLauncher)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    ava_home = tmp_path / "cluster-home"
    ctx = converge_host.ConvergeCtx(repo=tmp_path / "repo", ava_home=ava_home, roles=None)
    for step in _CLI_LINK_STEPS:
        step.apply(ctx)
    assert not ava_home.exists()
    assert not (tmp_path / "home").exists()


def _first_start_converge(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    prod: bool,
    existing_target: Path | None = None,
) -> tuple[Path, Path, Path]:
    """Run a scratch home's first-start CLI wiring; returns (checkout, home, bare link)."""
    assert len(_CLI_LINK_STEPS) == 2
    user_home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(user_home))
    ava_home = user_home / ".ava" if prod else user_home / ".ava-preview"
    checkout = ava_home / "source"
    checkout.mkdir(parents=True)
    bare_link = user_home / ".local" / "bin" / "ava"
    if existing_target is not None:
        bare_link.parent.mkdir(parents=True)
        bare_link.symlink_to(existing_target)
    converge_host.converge_host(
        checkout,
        frozenset({"gateway", "agent-runner"}),
        ava_home=ava_home,
        steps=_CLI_LINK_STEPS,
        services=frozenset(),
    )
    return checkout, ava_home, bare_link


def test_prod_first_start_links_bare_ava_to_the_launcher(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    checkout, ava_home, bare_link = _first_start_converge(tmp_path, monkeypatch, prod=True)

    assert bare_link.readlink() == checkout / "scripts" / "ava-launcher.sh"
    assert (ava_home / "ava").readlink() == checkout / ".venv" / "bin" / "ava"


def test_prod_first_start_repoints_a_checkout_link_to_the_launcher(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    old_target = tmp_path / "home" / ".ava" / "source" / ".venv" / "bin" / "ava"
    checkout, _, bare_link = _first_start_converge(
        tmp_path, monkeypatch, prod=True, existing_target=old_target
    )

    assert bare_link.readlink() == checkout / "scripts" / "ava-launcher.sh"


def test_non_prod_first_start_leaves_the_host_link_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prod_launcher = tmp_path / "prod" / "source" / "scripts" / "ava-launcher.sh"
    checkout, ava_home, bare_link = _first_start_converge(
        tmp_path, monkeypatch, prod=False, existing_target=prod_launcher
    )

    assert bare_link.readlink() == prod_launcher
    assert (ava_home / "ava").readlink() == checkout / ".venv" / "bin" / "ava"


def test_non_prod_first_start_creates_no_bare_link_when_none_existed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, _, bare_link = _first_start_converge(tmp_path, monkeypatch, prod=False)

    assert not bare_link.exists()
    assert not bare_link.is_symlink()


def test_missing_setup_error_names_the_checkout_cli(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`_print_missing_setup_error`'s first-time-setup example must name this
    checkout's own `.venv/bin/ava`, since the host-global `ava` cannot reach a
    home before its first start has linked `$AVA_HOME/ava`."""
    missing: list[_setup_commands._SetupField | _setup_commands._Capability] = [
        c for c in _setup_commands._CAPABILITIES if c.capability != "observability-station"
    ]
    _setup_commands._print_missing_setup_error(missing, None)
    err = capsys.readouterr().err
    assert f"{repo_root()}/.venv/bin/ava start --machine-name <name>" in err
