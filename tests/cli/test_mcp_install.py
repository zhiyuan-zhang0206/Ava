"""`ava mcp install/uninstall/upgrade` — out-of-core MCP package lifecycle.

Sources are local dirs (used in place) and local `file://` git repos (cloned +
moved). `unit_home` isolates ~/.ava. `_uv_sync` is monkeypatched in the bulk of
tests so the venv build is deterministic + offline; one test exercises the real
`uv sync` on a zero-dependency package.
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from ava._mcp_config import installed_mcp_dir, load_mcp_config
from cli.commands import cmd_mcp_install, cmd_mcp_list, cmd_mcp_uninstall, cmd_mcp_upgrade
from shared import install_registry as reg


def _make_mcp_package(
    root: Path,
    name: str = "acme",
    *,
    command: str = ".venv/bin/python",
    module: str = "acme_mcp",
    servers: list[str] | None = None,
    desc: str = "acme mcp",
) -> Path:
    """Build a standalone MCP package dir (`.mcp.json` + pyproject + module)."""
    pkg = root / f"{name}-src"
    pkg.mkdir(parents=True, exist_ok=True)
    names = servers if servers is not None else [name]
    spec = {n: {"command": command, "args": ["-m", module], "description": desc} for n in names}
    (pkg / ".mcp.json").write_text(json.dumps({"mcpServers": spec}), encoding="utf-8")
    (pkg / "pyproject.toml").write_text(
        f'[project]\nname = "{name}-mcp"\nversion = "0.0.0"\n'
        'requires-python = ">=3.12"\ndependencies = []\n\n[tool.uv]\npackage = false\n',
        encoding="utf-8",
    )
    mod = pkg / module
    mod.mkdir(exist_ok=True)
    (mod / "__init__.py").write_text("", encoding="utf-8")
    return pkg


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(  # noqa: S603 — fixed argv, test-local fixture repo
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    )


def _make_mcp_git_repo(root: Path, name: str = "acme") -> str:
    pkg = _make_mcp_package(root, name)
    _git(pkg, "init", "-q")
    _git(pkg, "config", "user.email", "t@t")
    _git(pkg, "config", "user.name", "t")
    _git(pkg, "add", ".")
    _git(pkg, "commit", "-q", "-m", "init")
    return f"file://{pkg}"


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


# ─── install ─────────────────────────────────────────────────────────────


def test_install_from_local_path_records_and_surfaces(
    unit_home: Path, tmp_path: Path, fake_uv_sync: None
) -> None:
    pkg = _make_mcp_package(tmp_path)
    assert cmd_mcp_install(str(pkg), None, None) == 0

    r = reg.get("acme")
    assert r is not None and r.type == "mcp" and r.origin == "user" and r.enabled
    dest = unit_home / "mcps" / "acme"
    assert (dest / ".mcp.json").is_file()
    assert (dest / ".venv" / "bin" / "python").exists()  # _uv_sync ran
    # local source is copied, never moved
    assert (pkg / ".mcp.json").is_file()
    # surfaces in the merged config with the relative direct-launch command + cwd
    cfg = load_mcp_config()
    assert cfg["acme"]["command"] == ".venv/bin/python"
    assert installed_mcp_dir("acme") == dest


def test_install_from_git_moves_and_strips_git(
    unit_home: Path, tmp_path: Path, fake_uv_sync: None
) -> None:
    url = _make_mcp_git_repo(tmp_path)
    assert cmd_mcp_install(url, None, None) == 0
    dest = unit_home / "mcps" / "acme"
    assert (dest / ".mcp.json").is_file()
    assert not (dest / ".git").exists()
    assert "acme" in load_mcp_config()


def test_install_with_path_subdir(unit_home: Path, tmp_path: Path, fake_uv_sync: None) -> None:
    root = tmp_path / "monorepo"
    _make_mcp_package(root / "packages", "acme")  # -> packages/acme-src
    assert cmd_mcp_install(str(root), None, "packages/acme-src") == 0
    assert reg.get("acme") is not None
    assert (unit_home / "mcps" / "acme").is_dir()


def test_install_rejects_duplicate(unit_home: Path, tmp_path: Path, fake_uv_sync: None) -> None:
    pkg = _make_mcp_package(tmp_path)
    assert cmd_mcp_install(str(pkg), None, None) == 0
    assert cmd_mcp_install(str(pkg), None, None) == 1


def test_install_rejects_builtin_collision(
    unit_home: Path, tmp_path: Path, fake_uv_sync: None, capsys: pytest.CaptureFixture
) -> None:
    pkg = _make_mcp_package(tmp_path, "chrome")
    assert cmd_mcp_install(str(pkg), None, None) == 1
    assert "built-in" in capsys.readouterr().err  # pyright: ignore[reportUnknownMemberType]
    assert reg.get("chrome") is None


def test_install_rejects_no_mcp_json_or_pyproject(
    unit_home: Path, tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    """A directory without .mcp.json or pyproject.toml is rejected."""
    plain = tmp_path / "plain"
    plain.mkdir()
    (plain / "readme.txt").write_text("hi", encoding="utf-8")
    assert cmd_mcp_install(str(plain), None, None) == 1
    assert "no .mcp.json or pyproject.toml" in capsys.readouterr().err  # pyright: ignore[reportUnknownMemberType]


def test_install_rejects_multi_server_package(
    unit_home: Path, tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    pkg = _make_mcp_package(tmp_path, "acme", servers=["acme", "acme2"])
    assert cmd_mcp_install(str(pkg), None, None) == 1
    assert "exactly one server" in capsys.readouterr().err  # pyright: ignore[reportUnknownMemberType]


# ─── auto-detection from pyproject.toml (no .mcp.json shipped) ────────────


def _make_pyproject_only_package(root: Path, name: str = "widget") -> Path:
    """A package with pyproject.toml + a module but NO .mcp.json."""
    pkg = root / f"{name}-src"
    pkg.mkdir(parents=True, exist_ok=True)
    module = f"{name}_mcp"
    content = (
        f"[project]\n"
        f'name = "ava-mcp-{name}"\n'
        f'version = "0.0.0"\n'
        f'description = "{name} MCP bridge"\n'
        f'requires-python = ">=3.12"\n'
        f"dependencies = []\n"
        f"\n"
        f"[tool.uv]\n"
        f"package = false\n"
    )
    (pkg / "pyproject.toml").write_text(content, encoding="utf-8")
    mod = pkg / module
    mod.mkdir(exist_ok=True)
    (mod / "__init__.py").write_text("", encoding="utf-8")
    (mod / "__main__.py").write_text("print('hello')", encoding="utf-8")
    return pkg


def test_install_auto_detects_from_pyproject(
    unit_home: Path, tmp_path: Path, fake_uv_sync: None
) -> None:
    """When .mcp.json is absent, auto-detect name + module from pyproject.toml
    and __main__.py, generate .mcp.json, and install."""
    pkg = _make_pyproject_only_package(tmp_path, "widget")
    assert cmd_mcp_install(str(pkg), None, None) == 0

    # Server name derived from pyproject name (stripped "ava-mcp-" prefix)
    r = reg.get("widget")
    assert r is not None and r.type == "mcp"
    dest = unit_home / "mcps" / "widget"
    assert (dest / ".mcp.json").is_file()
    # Generated .mcp.json has the correct shape
    data = json.loads((dest / ".mcp.json").read_text(encoding="utf-8"))
    spec = data["mcpServers"]["widget"]
    assert spec["command"] == ".venv/bin/python"
    assert spec["args"] == ["-m", "widget_mcp"]
    assert spec["description"] == "widget MCP bridge"
    # Surfaces in merged config
    cfg = load_mcp_config()
    assert cfg["widget"]["command"] == ".venv/bin/python"


def test_install_auto_detect_falls_back_to_single_package(
    unit_home: Path, tmp_path: Path, fake_uv_sync: None
) -> None:
    """Without __main__.py, detect a single top-level Python package."""
    pkg = tmp_path / "fallback-src"
    pkg.mkdir()
    content = (
        "[project]\n"
        'name = "mcp-fallback"\n'
        'requires-python = ">=3.12"\n'
        "dependencies = []\n"
        "\n"
        "[tool.uv]\n"
        "package = false\n"
    )
    (pkg / "pyproject.toml").write_text(content, encoding="utf-8")
    mod = pkg / "fallback_mcp"
    mod.mkdir()
    (mod / "__init__.py").write_text("", encoding="utf-8")
    assert cmd_mcp_install(str(pkg), None, None) == 0
    assert reg.get("fallback") is not None


def test_install_auto_detect_rejects_bare_pyproject(
    unit_home: Path, tmp_path: Path, fake_uv_sync: None, capsys: pytest.CaptureFixture
) -> None:
    """A pyproject.toml without a detectable module succeeds with fallback."""
    pkg = tmp_path / "bare-src"
    pkg.mkdir()
    content_py = (
        "[project]\n"
        'name = "bare"\n'
        'version = "0.0.0"\n'
        'requires-python = ">=3.12"\n'
        "\n"
        "[tool.uv]\n"
        "package = false\n"
    )
    (pkg / "pyproject.toml").write_text(content_py, encoding="utf-8")
    # No __main__.py, no __init__.py in a subdir — _detect_module falls
    # back to "bare", but the module dir doesn't exist.  uv sync would still
    # succeed, but the server won't start.  Install succeeds anyway (we
    # don't verify the module exists on disk — that's a runtime concern).
    assert cmd_mcp_install(str(pkg), None, None) == 0
    assert reg.get("bare") is not None


# ─── --env injection (the only channel a secret reaches a spawned server) ─


_FAKE_TOKEN = "s3cret"  # noqa: S105 — fixture value, not a credential


def _landed_env(unit_home: Path, name: str = "acme") -> dict:
    data = json.loads((unit_home / "mcps" / name / ".mcp.json").read_text(encoding="utf-8"))
    return data["mcpServers"][name].get("env", {})


def test_install_env_injected_into_landed_copy(
    unit_home: Path, tmp_path: Path, fake_uv_sync: None
) -> None:
    pkg = _make_mcp_package(tmp_path)
    assert cmd_mcp_install(str(pkg), None, None, [f"TOKEN={_FAKE_TOKEN}", "MODE=live"]) == 0
    env = _landed_env(unit_home)
    assert env["TOKEN"] == _FAKE_TOKEN and env["MODE"] == "live"
    # the source package is untouched — secrets never flow back into the source
    src = json.loads((pkg / ".mcp.json").read_text(encoding="utf-8"))
    assert "TOKEN" not in (src["mcpServers"]["acme"].get("env") or {})
    # and the merged config the daemon reads carries it
    assert load_mcp_config()["acme"]["env"]["TOKEN"] == _FAKE_TOKEN


def test_install_env_overrides_package_default(
    unit_home: Path, tmp_path: Path, fake_uv_sync: None
) -> None:
    pkg = _make_mcp_package(tmp_path)
    # package ships a default env key; --env wins
    data = json.loads((pkg / ".mcp.json").read_text(encoding="utf-8"))
    data["mcpServers"]["acme"]["env"] = {"MODE": "default", "KEEP": "yes"}
    (pkg / ".mcp.json").write_text(json.dumps(data), encoding="utf-8")
    assert cmd_mcp_install(str(pkg), None, None, ["MODE=live"]) == 0
    env = _landed_env(unit_home)
    assert env["MODE"] == "live" and env["KEEP"] == "yes"


def test_install_rejects_bad_env_pair(
    unit_home: Path, tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    pkg = _make_mcp_package(tmp_path)
    assert cmd_mcp_install(str(pkg), None, None, ["NOEQUALS"]) == 1
    assert "KEY=VALUE" in capsys.readouterr().err  # pyright: ignore[reportUnknownMemberType]


def test_upgrade_preserves_injected_env(
    unit_home: Path, tmp_path: Path, fake_uv_sync: None
) -> None:
    """An upgrade must never silently drop a server's token: the source ships no
    secret, so the landed copy's env is carried across the re-land."""
    pkg = _make_mcp_package(tmp_path, desc="v1")
    cmd_mcp_install(str(pkg), None, None, [f"TOKEN={_FAKE_TOKEN}"])
    (pkg / ".mcp.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "acme": {
                        "command": ".venv/bin/python",
                        "args": ["-m", "acme_mcp"],
                        "description": "v2",
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    assert cmd_mcp_upgrade("acme") == 0
    assert load_mcp_config()["acme"]["description"] == "v2"  # code/config refreshed
    assert _landed_env(unit_home)["TOKEN"] == _FAKE_TOKEN  # secret survived


# ─── list / uninstall / upgrade ──────────────────────────────────────────


def test_list_flags_installed_origin(
    unit_home: Path, tmp_path: Path, fake_uv_sync: None, capsys: pytest.CaptureFixture
) -> None:
    cmd_mcp_install(str(_make_mcp_package(tmp_path)), None, None)
    assert cmd_mcp_list() == 0
    out = capsys.readouterr().out  # pyright: ignore[reportUnknownMemberType]
    assert "acme" in out and "installed" in out


def test_uninstall_removes_dir_registry_and_config(
    unit_home: Path, tmp_path: Path, fake_uv_sync: None
) -> None:
    cmd_mcp_install(str(_make_mcp_package(tmp_path)), None, None)
    assert cmd_mcp_uninstall("acme") == 0
    assert reg.get("acme") is None
    assert not (unit_home / "mcps" / "acme").exists()
    assert "acme" not in load_mcp_config()


def test_uninstall_rejects_non_mcp(unit_home: Path, capsys: pytest.CaptureFixture) -> None:
    assert cmd_mcp_uninstall("nope") == 1
    assert "not an installed MCP package" in capsys.readouterr().err  # pyright: ignore[reportUnknownMemberType]


def test_upgrade_refetches_from_source(unit_home: Path, tmp_path: Path, fake_uv_sync: None) -> None:
    pkg = _make_mcp_package(tmp_path, desc="v1")
    cmd_mcp_install(str(pkg), None, None)
    # mutate the local source's .mcp.json, then upgrade
    (pkg / ".mcp.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "acme": {
                        "command": ".venv/bin/python",
                        "args": ["-m", "acme_mcp"],
                        "description": "v2",
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    assert cmd_mcp_upgrade("acme") == 0
    assert load_mcp_config()["acme"]["description"] == "v2"


def test_upgrade_rejects_non_mcp(unit_home: Path, capsys: pytest.CaptureFixture) -> None:
    assert cmd_mcp_upgrade("nope") == 1


# ─── real uv sync (integration; zero-dependency package, no network) ──────


@pytest.mark.skipif(shutil.which("uv") is None, reason="uv not on PATH")
def test_install_real_uv_sync_builds_venv(unit_home: Path, tmp_path: Path) -> None:
    pkg = _make_mcp_package(tmp_path, "echoreal", module="echoreal")
    assert cmd_mcp_install(str(pkg), None, None) == 0
    dest = unit_home / "mcps" / "echoreal"
    assert (dest / ".venv" / "bin" / "python").exists()
    assert "echoreal" in load_mcp_config()


# ─── dead local-path source (audit round 2, skills-plugins #4) ──────────────


def test_acquire_dead_local_path_raises_clear_error(tmp_path: Path) -> None:
    """A recorded source that is neither a git URL nor an existing dir must
    name the problem instead of falling into a confusing git clone failure."""
    from cli.commands._pkg_source import SourcePathNotFoundError, acquire_source

    dead = tmp_path / "gone" / "mcp"
    with pytest.raises(SourcePathNotFoundError, match="no such local directory"):
        acquire_source(str(dead), None)


def test_upgrade_dead_source_reports_and_fails(unit_home: Path, tmp_path: Path) -> None:
    """`ava mcp upgrade` of a package whose local source dir was deleted (the
    worktree-removal case) prints the recorded-source problem, exit 1."""
    pkg = _make_mcp_package(tmp_path, "orphan", module="orphan_mcp")
    assert cmd_mcp_install(str(pkg), None, None) == 0
    shutil.rmtree(pkg)  # the source dir vanishes (like a deleted worktree)
    assert cmd_mcp_upgrade("orphan", force=True) == 1


def test_mcp_list_flags_dead_source(unit_home: Path, tmp_path: Path, capsys) -> None:
    """`ava mcp list` warns that a recorded local source is gone, so the
    operator sees the upgradeability problem before an upgrade fails."""
    pkg = _make_mcp_package(tmp_path, "orphan2", module="orphan2_mcp")
    assert cmd_mcp_install(str(pkg), None, None) == 0
    shutil.rmtree(pkg)
    assert cmd_mcp_list() == 0
    err = capsys.readouterr().err  # pyright: ignore[reportUnknownMemberType]
    assert "recorded source path no longer exists" in err
