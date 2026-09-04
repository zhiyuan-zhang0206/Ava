from __future__ import annotations

import json
import stat
from pathlib import Path
from typing import Any, cast

import pytest

from cli.commands import _converge
from cli.commands import _converge_external_agent_skills as bridge
from cli.commands import _external_agent_skill_fs as bridge_fs

SKILL = "operating-ava-cluster"


def _world(tmp_path: Path) -> tuple[Path, Path, _converge.ConvergeCtx]:
    repo = tmp_path / "repo"
    source = repo / ".agents" / "skills" / SKILL
    source.mkdir(parents=True)
    (source / "SKILL.md").write_text("operator v1\n")
    host_home = tmp_path / "host-home"
    client = host_home / ".codex"
    client.mkdir(parents=True)
    ava_home = tmp_path / "ava-home"
    (ava_home / "configs").mkdir(parents=True)
    return source, client, _converge.ConvergeCtx(repo=repo, ava_home=ava_home, roles=None)


def _target(client: Path) -> Path:
    return client / "skills" / SKILL


def _ledger(context: _converge.ConvergeCtx) -> dict[str, Any]:
    value: object = json.loads(
        (context.ava_home / "configs" / "external-agent-skills" / "codex.json").read_text()
    )
    return cast(dict[str, Any], value)


def _residues(client: Path) -> list[Path]:
    return list((client / "skills").glob(f".{SKILL}.ava-*"))


def test_restart_reconciles_stage_published_before_post_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, client, context = _world(tmp_path)
    target = _target(client)
    original_rename = bridge._rename_no_replace
    interrupted = False

    def interrupt_after_stage_publish(source_path: Path, destination: Path) -> None:
        nonlocal interrupted
        original_rename(source_path, destination)
        if destination.name.startswith(f".{SKILL}.ava-stage-") and not interrupted:
            interrupted = True
            raise SystemExit("process killed after stage publication")

    monkeypatch.setattr(bridge, "_rename_no_replace", interrupt_after_stage_publish)
    with pytest.raises(SystemExit, match="stage publication"):
        bridge.converge_external_agent_skill(context, host_home=client.parent)

    assert interrupted
    assert _ledger(context)["transaction"]["stage_state"] == "publishing"
    target.mkdir()
    (target / "user.txt").write_text("late user target\n")
    monkeypatch.setattr(bridge, "_rename_no_replace", original_rename)

    bridge.converge_external_agent_skill(context, host_home=client.parent)

    assert (target / "user.txt").read_text() == "late user target\n"
    assert _ledger(context)["installed"] is None
    (target / "user.txt").unlink()
    target.rmdir()
    bridge.converge_external_agent_skill(context, host_home=client.parent)

    assert (target / "SKILL.md").read_text() == "operator v1\n"
    assert _ledger(context)["transaction"] is None
    assert _residues(client) == []
    assert not list((context.ava_home / "configs" / "external-agent-skills").glob(".codex-stage-*"))


def test_restart_reconciles_claim_before_post_write_and_preserves_late_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, client, context = _world(tmp_path)
    bridge.converge_external_agent_skill(context, host_home=client.parent)
    target = _target(client)
    installed_before = _ledger(context)["installed"]
    source.joinpath("SKILL.md").write_text("operator v2\n")
    original_rename = bridge._rename_no_replace
    interrupted = False

    def interrupt_after_claim(source_path: Path, destination: Path) -> None:
        nonlocal interrupted
        original_rename(source_path, destination)
        if source_path == target and not interrupted:
            interrupted = True
            raise SystemExit("process killed after target claim")

    monkeypatch.setattr(bridge, "_rename_no_replace", interrupt_after_claim)
    with pytest.raises(SystemExit, match="target claim"):
        bridge.converge_external_agent_skill(context, host_home=client.parent)

    assert interrupted
    assert _ledger(context)["transaction"]["claim_state"] == "claiming"
    target.mkdir()
    (target / "user.txt").write_text("late user target\n")
    monkeypatch.setattr(bridge, "_rename_no_replace", original_rename)

    bridge.converge_external_agent_skill(context, host_home=client.parent)

    assert (target / "user.txt").read_text() == "late user target\n"
    ledger = _ledger(context)
    assert ledger["installed"] == installed_before
    assert ledger["transaction"]["claim_state"] == "claiming"
    (target / "user.txt").unlink()
    target.rmdir()
    bridge.converge_external_agent_skill(context, host_home=client.parent)

    assert (target / "SKILL.md").read_text() == "operator v2\n"
    assert _ledger(context)["transaction"] is None
    assert _residues(client) == []


def test_cleanup_claim_verifies_swapped_file_before_deletion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, client, context = _world(tmp_path)
    bridge.converge_external_agent_skill(context, host_home=client.parent)
    source.joinpath("SKILL.md").write_text("operator v2\n")
    outside = tmp_path / "outside.md"
    outside.write_text("outside\n")
    outside.chmod(0o444)
    outside_before = outside.stat()
    original_rename = bridge._rename_no_replace
    injected = False

    def swap_before_cleanup_claim(source_path: Path, destination: Path) -> None:
        nonlocal injected
        if ".ava-previous-" in source_path.name and ".ava-quarantine-" in destination.name:
            victim = source_path / "SKILL.md"
            victim.unlink()
            victim.symlink_to(outside)
            injected = True
        original_rename(source_path, destination)

    monkeypatch.setattr(bridge, "_rename_no_replace", swap_before_cleanup_claim)

    bridge.converge_external_agent_skill(context, host_home=client.parent)

    assert injected
    assert (_target(client) / "SKILL.md").read_text() == "operator v2\n"
    previous = next((client / "skills").glob(f".{SKILL}.ava-previous-*"))
    swapped = previous / "SKILL.md"
    assert swapped.is_symlink()
    assert outside.read_text() == "outside\n"
    assert stat.S_IMODE(outside.stat().st_mode) == stat.S_IMODE(outside_before.st_mode)
    assert _ledger(context)["garbage"][0]["location"] == "source"

    monkeypatch.setattr(bridge, "_rename_no_replace", original_rename)
    swapped.unlink()
    swapped.write_text("operator v1\n")
    bridge.converge_external_agent_skill(context, host_home=client.parent)

    assert _residues(client) == []
    assert _ledger(context)["garbage"] == []


def test_restart_reconciles_cleanup_claim_before_post_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, client, context = _world(tmp_path)
    bridge.converge_external_agent_skill(context, host_home=client.parent)
    source.joinpath("SKILL.md").write_text("operator v2\n")
    original_rename = bridge._rename_no_replace
    interrupted = False

    def interrupt_after_cleanup_claim(source_path: Path, destination: Path) -> None:
        nonlocal interrupted
        original_rename(source_path, destination)
        if ".ava-quarantine-" in destination.name and not interrupted:
            interrupted = True
            raise SystemExit("process killed after cleanup claim")

    monkeypatch.setattr(bridge, "_rename_no_replace", interrupt_after_cleanup_claim)
    with pytest.raises(SystemExit, match="cleanup claim"):
        bridge.converge_external_agent_skill(context, host_home=client.parent)

    assert interrupted
    assert _ledger(context)["garbage"][0]["location"] == "claiming"
    monkeypatch.setattr(bridge, "_rename_no_replace", original_rename)
    bridge.converge_external_agent_skill(context, host_home=client.parent)

    assert (_target(client) / "SKILL.md").read_text() == "operator v2\n"
    assert _ledger(context)["garbage"] == []
    assert _residues(client) == []


def test_directory_permission_change_is_bound_to_verified_inode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    residue = tmp_path / "residue"
    nested = residue / "nested"
    nested.mkdir(parents=True)
    nested.chmod(0o500)
    manifest = bridge_fs._tree_manifest(residue)
    outside = tmp_path / "outside"
    outside.mkdir()
    outside.chmod(0o755)
    outside_before = outside.stat()
    original_manifest = bridge_fs._tree_manifest
    original_lstat = bridge_fs._lstat
    armed = False
    swapped = False

    def arm_after_manifest(*args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        nonlocal armed
        result = original_manifest(*args, **kwargs)
        armed = True
        return result

    def swap_after_validation(path: Path):
        nonlocal armed, swapped
        current = original_lstat(path)
        if armed and path == nested:
            armed = False
            nested.rmdir()
            nested.symlink_to(outside, target_is_directory=True)
            swapped = True
        return current

    monkeypatch.setattr(bridge_fs, "_tree_manifest", arm_after_manifest)
    monkeypatch.setattr(bridge_fs, "_lstat", swap_after_validation)

    with pytest.raises(bridge_fs._ClientConflictError):
        bridge_fs._remove_manifest_subset(residue, manifest)

    assert swapped
    assert nested.is_symlink()
    assert stat.S_IMODE(outside.stat().st_mode) == stat.S_IMODE(outside_before.st_mode)


def test_file_unlink_uses_post_rename_identity_validation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    residue = tmp_path / "residue"
    residue.mkdir()
    victim = residue / "SKILL.md"
    victim.write_text("operator\n")
    manifest = bridge_fs._tree_manifest(residue)
    outside = tmp_path / "outside.md"
    outside.write_text("outside\n")
    outside.chmod(0o444)
    outside_before = outside.stat()
    original_rename = bridge_fs._rename_no_replace
    swapped = False

    def swap_before_file_claim(source: Path, destination: Path) -> None:
        nonlocal swapped
        if source == victim and ".ava-delete-" in destination.name:
            victim.unlink()
            victim.symlink_to(outside)
            swapped = True
        original_rename(source, destination)

    monkeypatch.setattr(bridge_fs, "_rename_no_replace", swap_before_file_claim)

    with pytest.raises(bridge_fs._ClientConflictError):
        bridge_fs._remove_manifest_subset(residue, manifest)

    assert swapped
    assert victim.is_symlink()
    assert outside.read_text() == "outside\n"
    assert stat.S_IMODE(outside.stat().st_mode) == stat.S_IMODE(outside_before.st_mode)
