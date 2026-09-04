from __future__ import annotations

import importlib
import json
from pathlib import Path

import pytest

from cli.commands import _converge

SKILL_NAME = "operating-ava-cluster"
MARKER_NAME = ".ava-managed.json"


@pytest.fixture
def home(tmp_path: Path) -> Path:
    host_home = tmp_path / "home"
    host_home.mkdir()
    assert host_home.resolve().is_relative_to(tmp_path.resolve())
    return host_home


def _bridge_module():
    return importlib.import_module("cli.commands._converge_external_agent_skills")


def _write_source(repo: Path, *, body: str = "operator v1\n") -> Path:
    source = repo / ".agents" / "skills" / SKILL_NAME
    (source / "references").mkdir(parents=True)
    (source / "SKILL.md").write_text(body)
    (source / "references" / "recovery.md").write_text("recover safely\n")
    return source


def _ctx(repo: Path, home: Path) -> _converge.ConvergeCtx:
    ava_home = home / ".ava"
    (ava_home / "configs").mkdir(parents=True, exist_ok=True)
    return _converge.ConvergeCtx(repo=repo, ava_home=ava_home, roles=None)


def _target(client_home: Path) -> Path:
    target = client_home / "skills" / SKILL_NAME
    assert target.resolve(strict=False).is_relative_to(client_home.parent.resolve())
    return target


def _run(repo: Path, home: Path) -> None:
    _bridge_module().converge_external_agent_skill(_ctx(repo, home), host_home=home)


def _tree_snapshot(root: Path) -> dict[str, tuple[str, bytes]]:
    snapshot: dict[str, tuple[str, bytes]] = {}
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        if path.is_dir():
            snapshot[relative] = ("dir", b"")
        else:
            snapshot[relative] = ("file", path.read_bytes())
    return snapshot


def _temp_entries(skills_root: Path) -> list[Path]:
    return list(skills_root.glob(f".{SKILL_NAME}.ava-*"))


def test_absent_client_homes_are_not_created(home: Path, tmp_path: Path) -> None:
    repo = tmp_path / "repo"

    _run(repo, home)

    assert not (home / ".codex").exists()
    assert not (home / ".claude").exists()


def test_present_codex_and_claude_homes_receive_complete_managed_copy(
    home: Path, tmp_path: Path
) -> None:
    repo = tmp_path / "repo"
    source = _write_source(repo)
    for client in (".codex", ".claude"):
        client_home = home / client
        client_home.mkdir()
        (client_home / "settings.json").write_text(f"{client} settings\n")

    _run(repo, home)

    for client in (".codex", ".claude"):
        client_home = home / client
        target = _target(client_home)
        assert (target / "SKILL.md").read_bytes() == (source / "SKILL.md").read_bytes()
        assert (target / "references" / "recovery.md").read_bytes() == (
            source / "references" / "recovery.md"
        ).read_bytes()
        marker = json.loads((target / MARKER_NAME).read_text())
        assert marker["owner"] == "ava"
        assert marker["skill"] == SKILL_NAME
        assert marker["format"] == 4
        assert len(marker["source_digest"]) == 64
        assert len(marker["installation_id"]) == 32
        assert len(marker["generation_id"]) == 32
        assert (client_home / "settings.json").read_text() == f"{client} settings\n"


def test_second_converge_is_byte_and_timestamp_idempotent(home: Path, tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _write_source(repo)
    client_home = home / ".codex"
    client_home.mkdir()
    module = _bridge_module()

    module.converge_external_agent_skill(_ctx(repo, home), host_home=home)
    target = _target(client_home)
    before = _tree_snapshot(target)
    mtimes = {path.relative_to(target): path.stat().st_mtime_ns for path in target.rglob("*")}

    module.converge_external_agent_skill(_ctx(repo, home), host_home=home)

    assert _tree_snapshot(target) == before
    assert {
        path.relative_to(target): path.stat().st_mtime_ns for path in target.rglob("*")
    } == mtimes
    assert _temp_entries(client_home / "skills") == []


def test_unmodified_managed_copy_updates_and_removes_only_stale_target_content(
    home: Path, tmp_path: Path
) -> None:
    repo = tmp_path / "repo"
    source = _write_source(repo)
    client_home = home / ".claude"
    client_home.mkdir()
    module = _bridge_module()
    module.converge_external_agent_skill(_ctx(repo, home), host_home=home)
    unrelated = client_home / "skills" / "personal-skill" / "notes.txt"
    unrelated.parent.mkdir()
    unrelated.write_text("user owned\n")

    (source / "SKILL.md").write_text("operator v2\n")
    (source / "references" / "recovery.md").unlink()
    (source / "references" / "workspace.md").write_text("workspace lookup\n")
    module.converge_external_agent_skill(_ctx(repo, home), host_home=home)

    target = _target(client_home)
    assert (target / "SKILL.md").read_text() == "operator v2\n"
    assert not (target / "references" / "recovery.md").exists()
    assert (target / "references" / "workspace.md").read_text() == "workspace lookup\n"
    assert unrelated.read_text() == "user owned\n"
    assert _temp_entries(client_home / "skills") == []


def test_unmanaged_preexisting_target_is_preserved_and_reported(
    home: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = tmp_path / "repo"
    _write_source(repo)
    client_home = home / ".codex"
    target = _target(client_home)
    target.mkdir(parents=True)
    (target / "SKILL.md").write_text("user version\n")
    before = _tree_snapshot(target)

    _run(repo, home)

    assert _tree_snapshot(target) == before
    assert "unmanaged" in capsys.readouterr().err
    assert _temp_entries(client_home / "skills") == []


def test_user_modified_managed_copy_is_preserved_and_reported(
    home: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = tmp_path / "repo"
    source = _write_source(repo)
    client_home = home / ".claude"
    client_home.mkdir()
    module = _bridge_module()
    module.converge_external_agent_skill(_ctx(repo, home), host_home=home)
    target = _target(client_home)
    (target / "SKILL.md").write_text("user customization\n")
    (target / "private-note.md").write_text("preserve me\n")
    (source / "SKILL.md").write_text("operator v2\n")
    before = _tree_snapshot(target)

    module.converge_external_agent_skill(_ctx(repo, home), host_home=home)

    assert _tree_snapshot(target) == before
    assert "user-modified" in capsys.readouterr().err
    assert _temp_entries(client_home / "skills") == []


def test_failed_update_restores_previous_copy_and_cleans_only_its_staging_dir(
    home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "repo"
    source = _write_source(repo)
    client_home = home / ".codex"
    client_home.mkdir()
    module = _bridge_module()
    module.converge_external_agent_skill(_ctx(repo, home), host_home=home)
    target = _target(client_home)
    before = _tree_snapshot(target)
    unrelated = client_home / "skills" / ".someone-elses-staging"
    unrelated.mkdir()
    (unrelated / "keep").write_text("untouched\n")
    (source / "SKILL.md").write_text("operator v2\n")
    original_rename = module._rename_no_replace

    def fail_staged_activation(path: Path, destination: Path) -> None:
        if path.name.startswith(f".{SKILL_NAME}.ava-stage-") and destination == target:
            raise OSError("injected activation failure")
        original_rename(path, destination)

    monkeypatch.setattr(module, "_rename_no_replace", fail_staged_activation)

    module.converge_external_agent_skill(_ctx(repo, home), host_home=home)

    assert _tree_snapshot(target) == before
    assert (unrelated / "keep").read_text() == "untouched\n"
    monkeypatch.setattr(module, "_rename_no_replace", original_rename)
    module.converge_external_agent_skill(_ctx(repo, home), host_home=home)
    assert (target / "SKILL.md").read_text() == "operator v2\n"
    assert _temp_entries(client_home / "skills") == []


def test_bridge_step_is_prod_host_global_and_skipped_for_dev_worktrees(
    home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _bridge_module()
    step = next(
        candidate
        for candidate in _converge.CONVERGE_STEPS
        if candidate.apply is module.converge_external_agent_skill
    )
    assert step.host_global
    assert not step.requires_unit_config
    assert step.roles == _converge.ALL_ROLES
    (home / ".codex").mkdir()

    def default_home(_path: Path) -> bool:
        return True

    monkeypatch.setattr(_converge, "is_default_home", default_home)
    monkeypatch.setattr(module.Path, "home", lambda: home)

    dev_repo = tmp_path / ".worktrees" / "feature"
    _write_source(dev_repo)
    _converge.converge_host(dev_repo, None, ava_home=home / ".ava", steps=(step,))
    assert not _target(home / ".codex").exists()

    prod_repo = tmp_path / "prod" / "source"
    _write_source(prod_repo)
    (home / ".ava" / "configs").mkdir(parents=True)
    _converge.converge_host(prod_repo, None, ava_home=home / ".ava", steps=(step,))
    assert _target(home / ".codex").is_dir()
