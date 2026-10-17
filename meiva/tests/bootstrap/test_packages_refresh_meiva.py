"""The skill scan drops packages the host contract blocks."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from base.packages.extensions import install_registry as reg
from base.paths.home import resolve_meiva_home


@pytest.fixture(autouse=True)
def _hermetic(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Isolated home, OS-job gate off (suite default)."""
    home = tmp_path / ".meiva"
    (home / "skills").mkdir(parents=True)
    (home / "logs").mkdir()
    monkeypatch.setenv("MEIVA_HOME", str(home))


def _home() -> Path:
    return resolve_meiva_home()


def _git(cwd: Path, *args: str, check: bool = True) -> str:
    env = {
        **os.environ,
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_SYSTEM": os.devnull,
    }
    result = subprocess.run(  # noqa: S603 — fixed argv, test-local fixture repo
        ["git", "-C", str(cwd), *args],
        check=check,
        capture_output=True,
        text=True,
        env=env,
    )
    return result.stdout


def _write_skill(
    repo: Path, name: str, body: str, manifest: dict[str, object] | None = None
) -> None:
    d = repo / "meiva_builtins" / "skills" / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: d\n---\n\n{body}", encoding="utf-8"
    )
    if manifest is not None:
        (d / "meiva-plugin.json").write_text(
            json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
        )


@pytest.fixture
def core_repo(tmp_path: Path) -> Path:
    bare = tmp_path / "origin.git"
    subprocess.run(  # noqa: S603 — fixed argv, test-local fixture repo
        ["git", "init", "--bare", "-q", "--initial-branch=main", str(bare)],
        check=True,
        capture_output=True,
    )
    repo = tmp_path / "repo"
    subprocess.run(  # noqa: S603 — fixed argv, test-local fixture repo
        ["git", "init", "-q", "--initial-branch=main", str(repo)],
        check=True,
        capture_output=True,
    )
    _git(repo, "config", "user.email", "test@example.invalid")
    _git(repo, "config", "user.name", "test")
    _write_skill(repo, "foo", "# v1\n")
    _write_skill(repo, "bar", "# v1\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "v1")
    _git(repo, "remote", "add", "origin", str(bare))
    _git(repo, "push", "-q", "-u", "origin", "main")
    return repo


def _manifest_for(name: str, **extra: object) -> dict[str, object]:
    data: dict[str, object] = {"apiVersion": 2, "name": name, "version": "1.0.0"}
    data.update(extra)
    return data


def test_scan_drops_host_blocked_packages(core_repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import base.paths as paths_mod
    from base.packages.extensions.install_registry import loadable_skill_names
    from meiva.skills.skill_index import scan_tree

    home = _home()
    for name in ("ok", "blocked"):
        d = home / "skills" / name
        d.mkdir(parents=True)
        (d / "SKILL.md").write_text(f"---\nname: {name}\ndescription: d\n---\n", encoding="utf-8")
        reg.register(reg.InstalledPackage(name=name, type="skill", enabled=True))
    (home / "skills" / "blocked" / "meiva-plugin.json").write_text(
        json.dumps(_manifest_for("blocked", engines={"meiva": ">=2099"})), encoding="utf-8"
    )
    monkeypatch.setattr(paths_mod, "repo_root", lambda: core_repo)

    tree = scan_tree(home / "skills", loadable_skill_names(), [])
    assert "ok" in tree and "blocked" not in tree
