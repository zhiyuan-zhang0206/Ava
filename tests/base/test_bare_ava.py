"""The host's bare `ava`: a plain link to the production checkout's CLI.

A production home's converge points `~/.local/bin/ava` at `<checkout>/.venv/bin/ava`
and writes no per-home CLI link; the home a run acts on is `AVA_HOME`, else
`~/.ava`, like every CLI. Until a home's first start has run, the hints name the
checkout's own CLI."""

from __future__ import annotations

from pathlib import Path

import pytest

from cli.commands.converge import _steps
from cli.commands.converge import host as converge_host

_REPO_ROOT = Path(__file__).resolve().parents[2]

# The converge step through which a first start (`_prepare_cold_start`) wires the
# CLI, taken from CONVERGE_STEPS with its real host-global gating.
_CLI_LINK_STEPS = tuple(step for step in converge_host.CONVERGE_STEPS if step.name == "ava on PATH")


def test_one_host_global_step_wires_the_cli() -> None:
    (step,) = _CLI_LINK_STEPS
    assert step.host_global
    assert not any("CLI link" in s.name for s in converge_host.CONVERGE_STEPS)
    assert not (_REPO_ROOT / "scripts" / "ava-launcher.sh").exists()


def test_cli_link_step_skips_hosts_without_the_symlink_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Windows has no `~/.local/bin/ava`: the link step writes nothing."""

    class _NoSymlink:
        def supports_ava_symlink(self) -> bool:
            return False

    monkeypatch.setattr(_steps, "get_backend", _NoSymlink)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    ctx = converge_host.ConvergeCtx(
        repo=tmp_path / "repo", ava_home=tmp_path / "cluster-home", roles=None
    )
    for step in _CLI_LINK_STEPS:
        step.apply(ctx)
    assert not (tmp_path / "cluster-home").exists()
    assert not (tmp_path / "home").exists()


def _first_start_converge(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    prod: bool,
    existing_target: Path | None = None,
) -> tuple[Path, Path, Path]:
    """Run a scratch home's first-start CLI wiring; returns (checkout, home, bare link)."""
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


def test_prod_first_start_links_bare_ava_to_the_checkout_cli(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    checkout, ava_home, bare_link = _first_start_converge(tmp_path, monkeypatch, prod=True)

    assert bare_link.readlink() == checkout / ".venv" / "bin" / "ava"
    assert not (ava_home / "ava").exists() and not (ava_home / "ava").is_symlink()


def test_prod_start_repoints_the_retired_launcher_link(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A host wired by the launcher is rewritten by its next start, with no manual step."""
    retired = tmp_path / "home" / ".ava" / "source" / "scripts" / "ava-launcher.sh"
    checkout, _, bare_link = _first_start_converge(
        tmp_path, monkeypatch, prod=True, existing_target=retired
    )

    assert bare_link.readlink() == checkout / ".venv" / "bin" / "ava"


def test_non_prod_first_start_leaves_the_host_link_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prod_cli = tmp_path / "prod" / "source" / ".venv" / "bin" / "ava"
    _, ava_home, bare_link = _first_start_converge(
        tmp_path, monkeypatch, prod=False, existing_target=prod_cli
    )

    assert bare_link.readlink() == prod_cli
    assert not (ava_home / "ava").exists() and not (ava_home / "ava").is_symlink()


def test_non_prod_first_start_creates_no_bare_link_when_none_existed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, _, bare_link = _first_start_converge(tmp_path, monkeypatch, prod=False)

    assert not bare_link.exists()
    assert not bare_link.is_symlink()
