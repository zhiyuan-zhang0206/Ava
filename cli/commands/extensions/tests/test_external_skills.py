from __future__ import annotations

import importlib
import json
import os
import stat
from collections.abc import Callable
from pathlib import Path

import pytest

from cli.commands.converge import spec as converge_host

SKILL_NAME = "operating-ava-cluster"


@pytest.fixture(autouse=True)
def single_operator_skill(monkeypatch: pytest.MonkeyPatch) -> None:
    module = importlib.import_module("cli.commands.extensions.external_skills")
    monkeypatch.setattr(module, "_SKILL_NAMES", ("operating-ava-cluster",), raising=False)


MARKER_NAME = ".ava-managed.json"


@pytest.fixture
def home(tmp_path: Path) -> Path:
    host_home = tmp_path / "home"
    host_home.mkdir()
    assert host_home.resolve().is_relative_to(tmp_path.resolve())
    return host_home


def _bridge_module():
    return importlib.import_module("cli.commands.extensions.external_skills")


def _filesystem_module():
    return importlib.import_module("cli.commands.extensions._external_skill_fs")


def _ntfs_lstat(
    inspect: Callable[[Path], os.stat_result],
) -> Callable[[Path], os.stat_result]:
    def inspect_with_synthetic_mode(path: Path) -> os.stat_result:
        current = inspect(path)
        values = list(current)
        if stat.S_ISREG(current.st_mode):
            values[stat.ST_MODE] = stat.S_IFREG | 0o666
        elif stat.S_ISDIR(current.st_mode):
            values[stat.ST_MODE] = stat.S_IFDIR | 0o777
        else:
            raise AssertionError(f"unexpected filesystem entry: {path}")
        return os.stat_result(values)

    return inspect_with_synthetic_mode


def _write_source(repo: Path, *, body: str = "operator v1\n") -> Path:
    source = repo / "ava_builtins" / "skills" / "platform" / SKILL_NAME
    (source / "references").mkdir(parents=True)
    (source / "SKILL.md").write_text(body)
    (source / "references" / "recovery.md").write_text("recover safely\n")
    return source


def _ctx(repo: Path, home: Path) -> converge_host.ConvergeCtx:
    ava_home = home / ".ava"
    (ava_home / "configs").mkdir(parents=True, exist_ok=True)
    return converge_host.ConvergeCtx(repo=repo, ava_home=ava_home, roles=None)


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
        assert marker["format"] == 6
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


def test_ntfs_synthetic_modes_do_not_break_converge(
    home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "repo"
    source = _write_source(repo)
    client_home = home / ".codex"
    client_home.mkdir()
    filesystem = _filesystem_module()
    monkeypatch.setattr(filesystem, "_is_posix", lambda: False)
    monkeypatch.setattr(filesystem, "_lstat", _ntfs_lstat(filesystem._lstat))
    monkeypatch.setattr(filesystem, "_source_lstat", _ntfs_lstat(filesystem._source_lstat))

    _run(repo, home)

    target = _target(client_home)
    assert (target / "SKILL.md").read_text() == "operator v1\n"

    (source / "SKILL.md").write_text("operator v2\n")
    _run(repo, home)

    assert (target / "SKILL.md").read_text() == "operator v2\n"
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


_DISTRIBUTED_SKILLS = ("operating-ava-cluster", "deploy-ava-cluster")


def _write_deploy_source(repo: Path) -> Path:
    source = repo / "ava_builtins" / "skills" / "platform" / "deploy-ava-cluster"
    source.mkdir(parents=True)
    (source / "SKILL.md").write_text("deployment v1\n")
    # A historical regular source lets the regression exercise the old installer.
    legacy = repo / ".agents" / "skills" / SKILL_NAME
    legacy.mkdir(parents=True)
    (legacy / "SKILL.md").write_text("operator v1\n")
    return source


@pytest.mark.parametrize("client", [".codex", ".claude"])
def test_both_operator_skills_install_and_update_independently(
    home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, client: str
) -> None:
    bridge = _bridge_module()
    monkeypatch.setattr(bridge, "_SKILL_NAMES", _DISTRIBUTED_SKILLS)
    repo = tmp_path / "repo"
    operate = _write_source(repo)
    deploy = _write_deploy_source(repo)
    client_home = home / client
    client_home.mkdir()
    _run(repo, home)
    targets = client_home / "skills"
    operate_before = _tree_snapshot(targets / SKILL_NAME)
    for name in _DISTRIBUTED_SKILLS:
        marker = json.loads((targets / name / MARKER_NAME).read_text())
        assert marker["skill"] == name
    (deploy / "SKILL.md").write_text("deployment v2\n")
    _run(repo, home)
    assert (targets / "deploy-ava-cluster" / "SKILL.md").read_text() == "deployment v2\n"
    assert _tree_snapshot(targets / SKILL_NAME) == operate_before
    (operate / "SKILL.md").write_text("operator v2\n")
    _run(repo, home)
    assert (targets / SKILL_NAME / "SKILL.md").read_text() == "operator v2\n"
    assert (targets / "deploy-ava-cluster" / "SKILL.md").read_text() == "deployment v2\n"
    ledger_root = home / ".ava" / "configs" / "external-agent-skills"
    key = "codex" if client == ".codex" else "claude"
    assert (ledger_root / f"{key}.json").is_file()
    assert (ledger_root / f"{key}-deploy-ava-cluster.json").is_file()


def test_operator_conflict_does_not_block_deployment_update(
    home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bridge = _bridge_module()
    monkeypatch.setattr(bridge, "_SKILL_NAMES", _DISTRIBUTED_SKILLS)
    repo = tmp_path / "repo"
    _write_source(repo)
    deploy = _write_deploy_source(repo)
    (home / ".codex").mkdir()
    _run(repo, home)
    target = _target(home / ".codex")
    (target / "SKILL.md").write_text("user customization\n")
    (deploy / "SKILL.md").write_text("deployment v2\n")
    _run(repo, home)
    assert (target / "SKILL.md").read_text() == "user customization\n"
    assert (target.parent / "deploy-ava-cluster" / "SKILL.md").read_text() == "deployment v2\n"


def test_deployment_publication_crash_recovers_without_changing_operator(
    home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bridge = _bridge_module()
    repo = tmp_path / "repo"
    _write_source(repo)
    _write_deploy_source(repo)
    (home / ".codex").mkdir()
    _run(repo, home)  # Legacy one-skill installation, with its original ledger filename.
    operator_before = _tree_snapshot(_target(home / ".codex"))
    monkeypatch.setattr(bridge, "_SKILL_NAMES", _DISTRIBUTED_SKILLS)
    original_rename = bridge._rename_no_replace

    def interrupt_publication(source: Path, destination: Path) -> None:
        original_rename(source, destination)
        if destination.name.startswith(".deploy-ava-cluster.ava-stage-"):
            raise SystemExit("interrupted deployment publication")

    monkeypatch.setattr(bridge, "_rename_no_replace", interrupt_publication)
    with pytest.raises(SystemExit, match="deployment publication"):
        _run(repo, home)
    monkeypatch.setattr(bridge, "_rename_no_replace", original_rename)
    _run(repo, home)
    assert _tree_snapshot(_target(home / ".codex")) == operator_before
    assert (
        home / ".codex" / "skills" / "deploy-ava-cluster" / "SKILL.md"
    ).read_text() == "deployment v1\n"


def test_product_guide_has_one_builtin_source_and_nested_operator_entries() -> None:
    from cli.commands.extensions.skills_sync import iter_sources

    repo = Path(__file__).resolve().parents[4]
    sources, _ = iter_sources(repo)
    canonical = repo / "ava_builtins" / "skills" / "platform" / "ava-guide"
    assert (repo / ".agents" / "skills" / "ava-guide").resolve() == canonical
    assert any(source.name == "ava-guide" and source.src == canonical for source in sources)
    assert not any(source.name in _DISTRIBUTED_SKILLS for source in sources)
    guide = (canonical / "SKILL.md").read_text()
    assert "deploy/SKILL.md" in guide
    assert "operations/SKILL.md" in guide


def test_builtin_guide_installs_with_the_core_update_channel(
    tmp_path: Path, unit_home: Path
) -> None:
    import shutil

    import ava.skills
    from base.packages.extensions import install_registry
    from cli.commands.extensions.skills_sync import converge_skills

    product_repo = Path(__file__).resolve().parents[4]
    repo = tmp_path / "repo"
    sources = repo / "ava_builtins" / "skills" / "platform"
    for name in ("ava-guide",):
        shutil.copytree(
            product_repo / "ava_builtins" / "skills" / "platform" / name, sources / name
        )
    converge_skills(repo, unit_home)
    assert any(skill["name"] == "ava-guide" for skill in ava.skills.names())
    for name in ("ava-guide",):
        entry = install_registry.get(name)
        assert entry is not None and entry.enabled and entry.origin == "repo"
        installed = unit_home / "skills" / name / "SKILL.md"
        original = installed.read_bytes()
        source = sources / name / "SKILL.md"
        source.write_bytes(original + b"\nUpdated product guidance.\n")
        converge_skills(repo, unit_home)
        assert installed.read_bytes() == original  # Built-ins update explicitly.
        policy = install_registry.resolved_policy(entry)
        assert policy.channel == "core" and policy.mode == "auto"
        assert entry.origin_path == str(sources / name)


def test_explicit_home_seam_never_calls_platform_home(
    home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bridge = _bridge_module()
    repo = tmp_path / "repo"
    _write_source(repo)
    (home / ".codex").mkdir()

    def real_home_forbidden() -> Path:
        raise AssertionError("tests must not resolve the real platform home")

    monkeypatch.setattr(bridge.Path, "home", real_home_forbidden)
    bridge.converge_external_agent_skill(_ctx(repo, home), host_home=home)
    assert _target(home / ".codex").is_dir()
