"""The loadable skill names follow the host contract of the installed-package registry."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from base.host.env.dotenv_boot import resolve_ava_home
from base.packages.extensions import install_registry as reg


@pytest.fixture(autouse=True)
def _hermetic(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Isolated home, OS-job gate off (suite default)."""
    home = tmp_path / ".ava"
    (home / "skills").mkdir(parents=True)
    (home / "logs").mkdir()
    monkeypatch.setenv("AVA_HOME", str(home))


def _home() -> Path:
    return resolve_ava_home()


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
    d = repo / "ava_builtins" / "skills" / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: d\n---\n\n{body}", encoding="utf-8"
    )
    if manifest is not None:
        (d / "ava-plugin.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")


def _head(repo: Path) -> str:
    return _git(repo, "rev-parse", "HEAD").strip()


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


def test_loadable_names_respect_the_host_contract(
    core_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from base.packages.extensions.install_registry import (
        host_contract_reason,
        loadable_skill_names,
    )

    home = _home()

    def skill(name: str, manifest: dict[str, object] | None) -> None:
        d = home / "skills" / name
        d.mkdir(parents=True)
        (d / "SKILL.md").write_text(f"---\nname: {name}\ndescription: d\n---\n", encoding="utf-8")
        if manifest is not None:
            (d / "ava-plugin.json").write_text(json.dumps(manifest), encoding="utf-8")
        reg.register(reg.InstalledPackage(name=name, type="skill", enabled=True))

    head = _head(core_repo)
    # a side-branch commit exists but is not an ancestor of main
    _git(core_repo, "checkout", "-qb", "side")
    (core_repo / "side.txt").write_text("side\n", encoding="utf-8")
    _git(core_repo, "add", "side.txt")
    _git(core_repo, "commit", "-qm", "side")
    side = _head(core_repo)
    _git(core_repo, "checkout", "-q", "main")

    skill("plain", None)
    skill("ok", _manifest_for("ok", requires_commit=head))
    skill("future", _manifest_for("future", requires_commit="d" * 40))
    skill("side", _manifest_for("side", requires_commit=side))
    skill("too_new", _manifest_for("too_new", engines={"ava": ">=2099"}))

    import base.paths as paths_mod

    monkeypatch.setattr(paths_mod, "repo_root", lambda: core_repo)
    loadable = loadable_skill_names()
    assert "plain" in loadable and "ok" in loadable
    assert "future" not in loadable and "side" not in loadable and "too_new" not in loadable
    assert "unresolvable" in (host_contract_reason(home / "skills" / "future") or "")
    assert "not an ancestor" in (host_contract_reason(home / "skills" / "side") or "")
