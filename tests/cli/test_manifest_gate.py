"""Install-time manifest gate across the install/upgrade entries (S1–S2).

A package that ships `ava-plugin.json` is held to its contract before anything
is landed: invalid manifest / unsatisfied `engines.ava` / a pyproject that
breaks the `dependencies.pythonPackages` mirror all refuse the install with a
clear error, and nothing is written. Packages without a manifest keep the
legacy paths (covered by the existing install tests).
"""

import json
import subprocess
from pathlib import Path

import pytest

from cli.commands import cmd_mcp_install, cmd_mcp_upgrade, cmd_plugins_install, cmd_skill_install
from shared import install_registry as reg

# Every test here installs a package, which records `local:<machine>` provenance
# in the cluster registry — that needs a machine identity, which a bare
# `unit_home` deliberately lacks. See the fixture's docstring.
pytestmark = pytest.mark.usefixtures("_installed_machine_identity")

GOOD_MANIFEST = {
    "apiVersion": 2,
    "name": "acme",
    "version": "1.0.0",
    "description": "test package",
    "engines": {"ava": ">=0.1.0"},
    "dependencies": {"pythonPackages": ["mcp>=1.27,<2"]},
    "lifecycle": {"entry": "plugin.py", "activation": "immediate", "dispose": "effect-registry"},
}


def _write_manifest(pkg: Path, manifest: dict) -> None:
    (pkg / "ava-plugin.json").write_text(json.dumps(manifest), encoding="utf-8")


def _make_mcp_package(root: Path, name: str = "acme") -> Path:
    pkg = root / f"{name}-src"
    pkg.mkdir(parents=True, exist_ok=True)
    spec = {name: {"command": ".venv/bin/python", "args": ["-m", "acme_mcp"], "description": "x"}}
    (pkg / ".mcp.json").write_text(json.dumps({"mcpServers": spec}), encoding="utf-8")
    (pkg / "pyproject.toml").write_text(
        f'[project]\nname = "{name}-mcp"\nversion = "0.0.0"\n'
        'requires-python = ">=3.12"\ndependencies = ["mcp>=1.27,<2"]\n\n[tool.uv]\npackage = false\n',
        encoding="utf-8",
    )
    (pkg / "acme_mcp").mkdir(exist_ok=True)
    (pkg / "acme_mcp" / "__init__.py").write_text("", encoding="utf-8")
    return pkg


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(  # noqa: S603 — fixed argv, test-local fixture repo
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    )


@pytest.fixture
def fake_uv_sync(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub `_uv_sync` to drop a fake `.venv/bin/python` — no real resolve."""

    def _fake(pkg_dir: Path) -> None:
        venv_bin = pkg_dir / ".venv" / "bin"
        venv_bin.mkdir(parents=True, exist_ok=True)
        py = venv_bin / "python"
        py.write_text("#!/bin/sh\n", encoding="utf-8")
        py.chmod(0o755)

    monkeypatch.setattr("cli.commands.mcp._uv_sync", _fake)


# ─── ava mcp install ───────────────────────────────────────────────────


def test_mcp_install_refuses_manifest_beyond_host_engines(
    unit_home: Path, tmp_path: Path, fake_uv_sync: None, capsys: pytest.CaptureFixture
) -> None:
    pkg = _make_mcp_package(tmp_path)
    bad = dict(GOOD_MANIFEST, engines={"ava": ">=99"})
    _write_manifest(pkg, bad)
    assert cmd_mcp_install(str(pkg), None, None) == 1
    assert "requires Ava >=99" in capsys.readouterr().err  # pyright: ignore[reportUnknownMemberType]
    assert reg.get("acme") is None
    assert not (unit_home / "mcps" / "acme").exists()


def test_mcp_install_refuses_unbounded_pyproject(
    unit_home: Path, tmp_path: Path, fake_uv_sync: None, capsys: pytest.CaptureFixture
) -> None:
    """The #1198 shape: pyproject without an upper bound, manifest with one."""
    pkg = _make_mcp_package(tmp_path)
    _write_manifest(pkg, GOOD_MANIFEST)
    (pkg / "pyproject.toml").write_text(
        '[project]\nname = "acme-mcp"\nversion = "0.0.0"\n'
        'requires-python = ">=3.12"\ndependencies = ["mcp>=1.27"]\n\n[tool.uv]\npackage = false\n',
        encoding="utf-8",
    )
    assert cmd_mcp_install(str(pkg), None, None) == 1
    assert "#1198" in capsys.readouterr().err  # pyright: ignore[reportUnknownMemberType]
    assert reg.get("acme") is None


def test_mcp_install_refuses_pyproject_out_of_declared_range(
    unit_home: Path, tmp_path: Path, fake_uv_sync: None, capsys: pytest.CaptureFixture
) -> None:
    pkg = _make_mcp_package(tmp_path)
    _write_manifest(pkg, GOOD_MANIFEST)
    (pkg / "pyproject.toml").write_text(
        '[project]\nname = "acme-mcp"\nversion = "0.0.0"\n'
        'requires-python = ">=3.12"\ndependencies = ["mcp>=1.27,<3"]\n\n[tool.uv]\npackage = false\n',
        encoding="utf-8",
    )
    assert cmd_mcp_install(str(pkg), None, None) == 1
    assert "reaches above" in capsys.readouterr().err  # pyright: ignore[reportUnknownMemberType]
    assert reg.get("acme") is None


def test_mcp_install_refuses_invalid_manifest(
    unit_home: Path, tmp_path: Path, fake_uv_sync: None, capsys: pytest.CaptureFixture
) -> None:
    pkg = _make_mcp_package(tmp_path)
    (pkg / "ava-plugin.json").write_text("not json", encoding="utf-8")
    assert cmd_mcp_install(str(pkg), None, None) == 1
    assert "not valid JSON" in capsys.readouterr().err  # pyright: ignore[reportUnknownMemberType]
    assert reg.get("acme") is None


def test_mcp_install_accepts_valid_manifest(
    unit_home: Path, tmp_path: Path, fake_uv_sync: None
) -> None:
    pkg = _make_mcp_package(tmp_path)
    _write_manifest(pkg, GOOD_MANIFEST)
    assert cmd_mcp_install(str(pkg), None, None) == 0
    row = reg.get("acme")
    assert row is not None
    assert (unit_home / "mcps" / "acme" / "ava-plugin.json").is_file()


def test_mcp_install_manifest_without_pyproject_fails_when_declaring_packages(
    unit_home: Path, tmp_path: Path, fake_uv_sync: None, capsys: pytest.CaptureFixture
) -> None:
    pkg = _make_mcp_package(tmp_path)
    _write_manifest(pkg, GOOD_MANIFEST)
    (pkg / "pyproject.toml").unlink()
    assert cmd_mcp_install(str(pkg), None, None) == 1
    assert "no pyproject.toml" in capsys.readouterr().err  # pyright: ignore[reportUnknownMemberType]


# ─── ava mcp upgrade ───────────────────────────────────────────────────


def test_mcp_upgrade_refuses_source_that_breaks_the_contract(
    unit_home: Path, tmp_path: Path, fake_uv_sync: None, capsys: pytest.CaptureFixture
) -> None:
    pkg = _make_mcp_package(tmp_path)
    _write_manifest(pkg, GOOD_MANIFEST)
    _git(pkg, "init", "-q")
    _git(pkg, "config", "user.email", "t@t")
    _git(pkg, "config", "user.name", "t")
    _git(pkg, "add", ".")
    _git(pkg, "commit", "-q", "-m", "init")
    url = f"file://{pkg}"
    assert cmd_mcp_install(url, None, None) == 0

    _write_manifest(pkg, dict(GOOD_MANIFEST, engines={"ava": ">=99"}))
    _git(pkg, "add", ".")
    _git(pkg, "commit", "-q", "-m", "break engines")
    assert cmd_mcp_upgrade("acme") == 1
    assert "requires Ava >=99" in capsys.readouterr().err  # pyright: ignore[reportUnknownMemberType]
    # the landed copy still carries the previous manifest — nothing was re-landed
    landed = unit_home / "mcps" / "acme" / "ava-plugin.json"
    assert json.loads(landed.read_text(encoding="utf-8"))["engines"] == {"ava": ">=0.1.0"}


# ─── ava plugins install / ava skill install ───────────────────────────


def _git_repo(pkg: Path) -> str:
    _git(pkg, "init", "-q")
    _git(pkg, "config", "user.email", "t@t")
    _git(pkg, "config", "user.name", "t")
    _git(pkg, "add", ".")
    _git(pkg, "commit", "-q", "-m", "init")
    return f"file://{pkg}"


def test_plugins_install_refuses_invalid_manifest_before_detection(
    unit_home: Path, tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "ava-plugin.json").write_text('{"apiVersion": 99}', encoding="utf-8")
    assert cmd_plugins_install(_git_repo(pkg), None, None) == 1
    err = capsys.readouterr().err  # pyright: ignore[reportUnknownMemberType]
    assert "apiVersion" in err
    assert "unrecognized package" not in err  # the gate runs first
    assert reg.get("pkg") is None


def test_plugins_install_accepts_manifest_with_valid_engines(
    unit_home: Path, tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    """A valid manifest alone does not make an otherwise-unrecognized package
    installable — the legacy detection still decides the payload kind."""
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    _write_manifest(pkg, dict(GOOD_MANIFEST, name="pkg"))
    assert cmd_plugins_install(_git_repo(pkg), None, None) == 1
    assert "unrecognized package" in capsys.readouterr().err  # pyright: ignore[reportUnknownMemberType]


def test_skill_install_refuses_invalid_manifest_before_discovery(
    unit_home: Path, tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    pkg = tmp_path / "skills-src"
    pkg.mkdir()
    (pkg / "ava-plugin.json").write_text('{"apiVersion": 2, "name": "x"}', encoding="utf-8")
    assert cmd_skill_install(str(pkg), None, None) == 1
    err = capsys.readouterr().err  # pyright: ignore[reportUnknownMemberType]
    assert "engines is required" in err
    assert reg.get("x") is None


def test_skill_install_valid_manifest_proceeds(unit_home: Path, tmp_path: Path) -> None:
    pkg = tmp_path / "skills-src"
    pkg.mkdir()
    _write_manifest(pkg, dict(GOOD_MANIFEST, name="src"))
    (pkg / "SKILL.md").write_text(
        "---\nname: greet\ndescription: greets\n---\n\n# greet\n", encoding="utf-8"
    )
    assert cmd_skill_install(str(pkg), None, None) == 0
    assert reg.get("greet") is not None
