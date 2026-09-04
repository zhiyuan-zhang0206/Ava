from __future__ import annotations

import json
import os
import stat
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from cli.commands import _converge
from cli.commands import _converge_external_agent_skills as bridge
from cli.commands import _external_agent_skill_fs as bridge_fs

SKILL = "operating-ava-cluster"


def _source(repo: Path, body: str = "operator v1\n") -> Path:
    source = repo / ".agents" / "skills" / SKILL
    (source / "references").mkdir(parents=True)
    (source / "SKILL.md").write_text(body)
    (source / "references" / "recovery.md").write_text("recover\n")
    return source


def _context(repo: Path, tmp_path: Path) -> _converge.ConvergeCtx:
    ava_home = tmp_path / "ava-home"
    (ava_home / "configs").mkdir(parents=True)
    return _converge.ConvergeCtx(repo=repo, ava_home=ava_home, roles=None)


def _client_home(tmp_path: Path, name: str = ".codex") -> Path:
    home = tmp_path / "host-home"
    home.mkdir(exist_ok=True)
    client = home / name
    client.mkdir(exist_ok=True)
    assert client.resolve().is_relative_to(tmp_path.resolve())
    return client


def _target(client: Path, tmp_path: Path) -> Path:
    target = client / "skills" / SKILL
    assert target.resolve(strict=False).is_relative_to(tmp_path.resolve())
    return target


def _ledger(context: _converge.ConvergeCtx) -> dict[str, Any]:
    return cast(
        dict[str, Any],
        json.loads(
            (context.ava_home / "configs" / "external-agent-skills" / "codex.json").read_text()
        ),
    )


def test_explicit_home_seam_never_calls_platform_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "repo"
    _source(repo)
    client = _client_home(tmp_path)
    host_home = client.parent

    def real_home_forbidden() -> Path:
        raise AssertionError("tests must not resolve the real platform home")

    monkeypatch.setattr(bridge.Path, "home", real_home_forbidden)

    bridge.converge_external_agent_skill(_context(repo, tmp_path), host_home=host_home)

    assert _target(client, tmp_path).is_dir()


@pytest.mark.parametrize("linked_component", ["client-home", "skills-root"])
def test_linked_external_roots_are_rejected_without_following(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    linked_component: str,
) -> None:
    repo = tmp_path / "repo"
    _source(repo)
    host_home = tmp_path / "host-home"
    host_home.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    client = host_home / ".codex"
    if linked_component == "client-home":
        client.symlink_to(outside, target_is_directory=True)
    else:
        client.mkdir()
        (client / "skills").symlink_to(outside, target_is_directory=True)
    bridge.converge_external_agent_skill(_context(repo, tmp_path), host_home=host_home)

    assert not (outside / SKILL).exists()
    assert "Codex" in capsys.readouterr().err


def test_linked_host_home_is_rejected_without_inspection(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = tmp_path / "repo"
    _source(repo)
    outside_home = tmp_path / "outside-home"
    (outside_home / ".codex").mkdir(parents=True)
    linked_home = tmp_path / "linked-home"
    linked_home.symlink_to(outside_home, target_is_directory=True)

    bridge.converge_external_agent_skill(_context(repo, tmp_path), host_home=linked_home)

    assert not (outside_home / ".codex" / "skills").exists()
    assert "host home" in capsys.readouterr().err


def test_source_tree_link_is_fatal_before_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "repo"
    source = _source(repo)
    outside = tmp_path / "outside.md"
    outside.write_text("outside\n")
    (source / "references" / "linked.md").symlink_to(outside)
    client = _client_home(tmp_path)
    with pytest.raises(RuntimeError, match="source"):
        bridge.converge_external_agent_skill(_context(repo, tmp_path), host_home=client.parent)

    assert not _target(client, tmp_path).exists()


def test_source_path_component_link_is_fatal_before_copy(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    actual_agents = tmp_path / "actual-agents"
    source = actual_agents / "skills" / SKILL
    source.mkdir(parents=True)
    (source / "SKILL.md").write_text("operator\n")
    repo.mkdir()
    (repo / ".agents").symlink_to(actual_agents, target_is_directory=True)
    client = _client_home(tmp_path)

    with pytest.raises(RuntimeError, match="source"):
        bridge.converge_external_agent_skill(_context(repo, tmp_path), host_home=client.parent)

    assert not _target(client, tmp_path).exists()


def test_linked_target_is_preserved_without_following(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = tmp_path / "repo"
    _source(repo)
    client = _client_home(tmp_path)
    outside = tmp_path / "outside-target"
    outside.mkdir()
    (outside / "SKILL.md").write_text("outside\n")
    target = _target(client, tmp_path)
    target.parent.mkdir()
    target.symlink_to(outside, target_is_directory=True)

    bridge.converge_external_agent_skill(_context(repo, tmp_path), host_home=client.parent)

    assert (outside / "SKILL.md").read_text() == "outside\n"
    assert "unmanaged" in capsys.readouterr().err


def test_windows_reparse_attribute_is_rejected() -> None:
    current = SimpleNamespace(st_file_attributes=0x400)

    assert bridge_fs._attributes_reparse(cast(Any, current))


def test_late_edit_between_check_and_claim_is_restored_not_overwritten(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = tmp_path / "repo"
    source = _source(repo)
    client = _client_home(tmp_path)
    context = _context(repo, tmp_path)
    bridge.converge_external_agent_skill(context, host_home=client.parent)
    target = _target(client, tmp_path)
    (source / "SKILL.md").write_text("operator v2\n")
    original_stage = bridge._stage_copy

    def edit_after_check(
        source_path: Path,
        source_manifest: list[dict[str, Any]],
        skills_root: Path,
        ledger_path: Path,
        ledger: dict[str, Any],
        source_digest: str,
    ) -> Path:
        staged = original_stage(
            source_path,
            source_manifest,
            skills_root,
            ledger_path,
            ledger,
            source_digest,
        )
        (target / "SKILL.md").write_text("late user edit\n")
        return staged

    monkeypatch.setattr(bridge, "_stage_copy", edit_after_check)

    bridge.converge_external_agent_skill(context, host_home=client.parent)

    assert (target / "SKILL.md").read_text() == "late user edit\n"
    assert "conflict" in capsys.readouterr().err


def test_target_appearing_after_claim_is_not_replaced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = tmp_path / "repo"
    source = _source(repo)
    client = _client_home(tmp_path)
    context = _context(repo, tmp_path)
    bridge.converge_external_agent_skill(context, host_home=client.parent)
    target = _target(client, tmp_path)
    (source / "SKILL.md").write_text("operator v2\n")
    outside = tmp_path / "late-target"
    outside.mkdir()
    (outside / "SKILL.md").write_text("late user target\n")
    original_rename = bridge._rename_no_replace

    def insert_target_then_rename(source_path: Path, destination: Path) -> None:
        if ".ava-stage-" in source_path.name and destination == target:
            destination.symlink_to(outside, target_is_directory=True)
        original_rename(source_path, destination)

    monkeypatch.setattr(bridge, "_rename_no_replace", insert_target_then_rename)

    bridge.converge_external_agent_skill(context, host_home=client.parent)

    assert target.is_symlink()
    assert (outside / "SKILL.md").read_text() == "late user target\n"
    assert "conflict" in capsys.readouterr().err


def test_late_previous_destination_during_claim_is_not_replaced_or_owned(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "repo"
    source = _source(repo)
    client = _client_home(tmp_path)
    context = _context(repo, tmp_path)
    bridge.converge_external_agent_skill(context, host_home=client.parent)
    target = _target(client, tmp_path)
    installed_before = _ledger(context)["installed"]
    (source / "SKILL.md").write_text("operator v2\n")
    original_rename = bridge._rename_no_replace
    collision: Path | None = None

    def collide_with_claim(source_path: Path, destination: Path) -> None:
        nonlocal collision
        if source_path == target:
            collision = destination
            destination.mkdir()
            (destination / "user.txt").write_text("not Ava owned\n")
        original_rename(source_path, destination)

    monkeypatch.setattr(bridge, "_rename_no_replace", collide_with_claim)

    bridge.converge_external_agent_skill(context, host_home=client.parent)

    assert collision is not None
    assert (collision / "user.txt").read_text() == "not Ava owned\n"
    assert (target / "SKILL.md").read_text() == "operator v1\n"
    ledger = _ledger(context)
    assert ledger["installed"] == installed_before
    assert ledger["transaction"]["claim_state"] == "claiming"
    assert ledger["garbage"] == []


def test_late_target_during_verification_restore_is_not_replaced_or_disowned(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "repo"
    source = _source(repo)
    client = _client_home(tmp_path)
    context = _context(repo, tmp_path)
    bridge.converge_external_agent_skill(context, host_home=client.parent)
    target = _target(client, tmp_path)
    installed_before = _ledger(context)["installed"]
    (source / "SKILL.md").write_text("operator v2\n")
    original_verify = bridge._verify_marker
    original_rename = bridge._rename_no_replace
    late_target = False

    def reject_claimed_previous(root: Path, installation_id: str, generation_id: str) -> None:
        if ".ava-previous-" in root.name:
            raise bridge._ClientConflictError("claimed copy changed")
        original_verify(root, installation_id, generation_id)

    def collide_with_restore(source_path: Path, destination: Path) -> None:
        nonlocal late_target
        if ".ava-previous-" in source_path.name and destination == target:
            late_target = True
            destination.mkdir()
            (destination / "user.txt").write_text("late user target\n")
        original_rename(source_path, destination)

    monkeypatch.setattr(bridge, "_verify_marker", reject_claimed_previous)
    monkeypatch.setattr(bridge, "_rename_no_replace", collide_with_restore)

    bridge.converge_external_agent_skill(context, host_home=client.parent)

    assert late_target
    assert (target / "user.txt").read_text() == "late user target\n"
    ledger = _ledger(context)
    assert ledger["installed"] == installed_before
    assert ledger["transaction"]["claim_state"] == "claimed"
    assert ledger["garbage"] == []


def test_late_target_during_activation_restore_is_not_replaced_or_disowned(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "repo"
    source = _source(repo)
    client = _client_home(tmp_path)
    context = _context(repo, tmp_path)
    bridge.converge_external_agent_skill(context, host_home=client.parent)
    target = _target(client, tmp_path)
    installed_before = _ledger(context)["installed"]
    (source / "SKILL.md").write_text("operator v2\n")
    original_rename = bridge._rename_no_replace
    late_target = False

    def fail_activation_then_collide_with_restore(source_path: Path, destination: Path) -> None:
        nonlocal late_target
        if ".ava-stage-" in source_path.name and destination == target:
            raise OSError("activation interrupted")
        if ".ava-previous-" in source_path.name and destination == target:
            late_target = True
            destination.mkdir()
            (destination / "user.txt").write_text("late user target\n")
        original_rename(source_path, destination)

    monkeypatch.setattr(bridge, "_rename_no_replace", fail_activation_then_collide_with_restore)

    bridge.converge_external_agent_skill(context, host_home=client.parent)

    assert late_target
    assert (target / "user.txt").read_text() == "late user target\n"
    ledger = _ledger(context)
    assert ledger["installed"] == installed_before
    assert ledger["transaction"]["claim_state"] == "claimed"
    assert ledger["garbage"] == []


def test_preexisting_generation_sibling_is_preserved_without_false_ownership(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "repo"
    source = _source(repo)
    client = _client_home(tmp_path)
    context = _context(repo, tmp_path)
    bridge.converge_external_agent_skill(context, host_home=client.parent)
    target = _target(client, tmp_path)
    installed_before = _ledger(context)["installed"]
    (source / "SKILL.md").write_text("operator v2\n")
    generation_id = "a" * 32
    sibling = target.parent / f".{SKILL}.ava-stage-{generation_id}"
    sibling.mkdir()
    (sibling / "user.txt").write_text("not Ava owned\n")
    monkeypatch.setattr(bridge.uuid, "uuid4", lambda: SimpleNamespace(hex=generation_id))

    bridge.converge_external_agent_skill(context, host_home=client.parent)

    assert (sibling / "user.txt").read_text() == "not Ava owned\n"
    assert (target / "SKILL.md").read_text() == "operator v1\n"
    ledger = _ledger(context)
    assert ledger["installed"] == installed_before
    assert ledger["transaction"]["stage_state"] == "publishing"
    assert ledger["garbage"] == []


def test_concurrent_converges_serialize_transaction_owned_absence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "repo"
    source = _source(repo)
    client = _client_home(tmp_path)
    context = _context(repo, tmp_path)
    bridge.converge_external_agent_skill(context, host_home=client.parent)
    target = _target(client, tmp_path)
    (source / "SKILL.md").write_text("operator v2\n")
    claimed = threading.Event()
    release = threading.Event()
    second_activated_during_claim = threading.Event()
    original_rename = bridge._rename_no_replace
    first_thread_id: int | None = None

    def pause_after_claim(path: Path, destination: Path) -> None:
        original_rename(path, destination)
        if path == target and not claimed.is_set():
            claimed.set()
            assert release.wait(5)
        if (
            Path(destination) == target
            and path != target
            and threading.get_ident() != first_thread_id
        ):
            second_activated_during_claim.set()

    monkeypatch.setattr(bridge, "_rename_no_replace", pause_after_claim)
    errors: list[BaseException] = []

    def run() -> None:
        try:
            bridge.converge_external_agent_skill(context, host_home=client.parent)
        except BaseException as exc:
            errors.append(exc)

    first = threading.Thread(target=run)
    second = threading.Thread(target=run)
    first.start()
    first_thread_id = first.ident
    assert claimed.wait(5)
    second.start()
    second_activated_during_claim.wait(1)
    release.set()
    first.join(5)
    second.join(5)

    assert not first.is_alive() and not second.is_alive()
    assert errors == []
    assert not second_activated_during_claim.is_set()
    assert (target / "SKILL.md").read_text() == "operator v2\n"


def test_marker_spoof_without_external_ledger_is_unmanaged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = tmp_path / "repo"
    _source(repo, "repo operator\n")
    client = _client_home(tmp_path)
    target = _target(client, tmp_path)
    target.mkdir(parents=True)
    (target / "SKILL.md").write_text("spoofed user skill\n")
    digest = bridge._tree_digest(target)
    (target / ".ava-managed.json").write_text(
        json.dumps(
            {
                "content_sha256": digest,
                "format": 1,
                "owner": "ava",
                "skill": SKILL,
            }
        )
    )

    bridge.converge_external_agent_skill(_context(repo, tmp_path), host_home=client.parent)

    assert (target / "SKILL.md").read_text() == "spoofed user skill\n"
    assert "unmanaged" in capsys.readouterr().err


@pytest.mark.skipif(not hasattr(Path, "chmod"), reason="filesystem mode support required")
def test_permission_only_modification_is_a_conflict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = tmp_path / "repo"
    source = _source(repo)
    client = _client_home(tmp_path)
    context = _context(repo, tmp_path)
    bridge.converge_external_agent_skill(context, host_home=client.parent)
    target = _target(client, tmp_path)
    skill_file = target / "SKILL.md"
    changed_mode = 0o600 if stat.S_IMODE(skill_file.stat().st_mode) != 0o600 else 0o644
    skill_file.chmod(changed_mode)
    (source / "SKILL.md").write_text("operator v2\n")

    bridge.converge_external_agent_skill(context, host_home=client.parent)

    assert (target / "SKILL.md").read_text() == "operator v1\n"
    assert stat.S_IMODE(skill_file.stat().st_mode) == changed_mode
    assert "conflict" in capsys.readouterr().err


def test_source_modes_are_materialized_and_recorded(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    source = _source(repo)
    references = source / "references"
    recovery = references / "recovery.md"
    references.chmod(0o555)
    recovery.chmod(0o444)
    client = _client_home(tmp_path)
    context = _context(repo, tmp_path)

    bridge.converge_external_agent_skill(context, host_home=client.parent)

    target = _target(client, tmp_path)
    assert stat.S_IMODE((target / "references").stat().st_mode) == 0o555
    assert stat.S_IMODE((target / "references" / "recovery.md").stat().st_mode) == 0o444
    (source / "SKILL.md").write_text("operator v2\n")

    bridge.converge_external_agent_skill(context, host_home=client.parent)

    assert (target / "SKILL.md").read_text() == "operator v2\n"
    assert list(target.parent.glob(f".{SKILL}.ava-*")) == []


def test_external_filesystem_failure_is_label_only_and_fail_soft(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = tmp_path / "repo"
    _source(repo)
    codex = _client_home(tmp_path)
    claude = codex.parent / ".claude"
    claude.mkdir()
    original_stage = bridge._stage_copy

    def inaccessible(
        source: Path,
        source_manifest: list[dict[str, Any]],
        skills_root: Path,
        ledger_path: Path,
        ledger: dict[str, Any],
        source_digest: str,
    ) -> Path:
        if skills_root.parent.name == ".codex":
            raise PermissionError("/secret/absolute/client/path")
        return original_stage(
            source, source_manifest, skills_root, ledger_path, ledger, source_digest
        )

    monkeypatch.setattr(bridge, "_stage_copy", inaccessible)

    bridge.converge_external_agent_skill(_context(repo, tmp_path), host_home=codex.parent)

    output = capsys.readouterr()
    assert "Codex" in output.err
    assert "PermissionError" in output.err
    assert _target(claude, tmp_path).is_dir()
    assert str(codex.parent) not in output.err + output.out


def test_cleanup_failure_after_activation_is_retried_without_rollback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = tmp_path / "repo"
    source = _source(repo)
    client = _client_home(tmp_path)
    context = _context(repo, tmp_path)
    bridge.converge_external_agent_skill(context, host_home=client.parent)
    target = _target(client, tmp_path)
    (source / "SKILL.md").write_text("operator v2\n")
    original_rename = bridge._rename_no_replace
    failed = False

    def fail_previous_once(source_path: Path, destination: Path) -> None:
        nonlocal failed
        if f".{SKILL}.ava-previous-" in source_path.name and not failed:
            failed = True
            raise PermissionError("cleanup denied")
        original_rename(source_path, destination)

    monkeypatch.setattr(bridge, "_rename_no_replace", fail_previous_once)
    bridge.converge_external_agent_skill(context, host_home=client.parent)
    monkeypatch.setattr(bridge, "_rename_no_replace", original_rename)
    bridge.converge_external_agent_skill(context, host_home=client.parent)

    assert (target / "SKILL.md").read_text() == "operator v2\n"
    assert list(target.parent.glob(f".{SKILL}.ava-*")) == []
    output = capsys.readouterr()
    assert str(client.parent) not in output.err + output.out


def test_interrupted_post_activation_commit_recovers_deterministically(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "repo"
    source = _source(repo)
    client = _client_home(tmp_path)
    context = _context(repo, tmp_path)
    bridge.converge_external_agent_skill(context, host_home=client.parent)
    target = _target(client, tmp_path)
    (source / "SKILL.md").write_text("operator v2\n")
    original_commit = bridge._commit_activation
    interrupted = False

    def interrupt_once(
        ledger_path: Path,
        ledger: dict[str, Any],
        transaction: dict[str, Any],
        previous: Path,
    ) -> None:
        nonlocal interrupted
        if not interrupted:
            interrupted = True
            raise OSError("simulated process interruption")
        return original_commit(ledger_path, ledger, transaction, previous)

    monkeypatch.setattr(bridge, "_commit_activation", interrupt_once)
    bridge.converge_external_agent_skill(context, host_home=client.parent)
    assert (target / "SKILL.md").read_text() == "operator v2\n"

    monkeypatch.setattr(bridge, "_commit_activation", original_commit)
    bridge.converge_external_agent_skill(context, host_home=client.parent)

    assert (target / "SKILL.md").read_text() == "operator v2\n"
    assert list(target.parent.glob(f".{SKILL}.ava-*")) == []


def test_partial_stage_copy_remains_tracked_until_cleanup_finishes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "repo"
    _source(repo)
    client = _client_home(tmp_path)
    context = _context(repo, tmp_path)
    original_copy = bridge._copy_source_contents
    failed = False

    def fail_after_one_entry(source: Path, destination: Path) -> None:
        nonlocal failed
        if not failed:
            failed = True
            (destination / "SKILL.md").write_bytes((source / "SKILL.md").read_bytes())
            raise PermissionError("copy interrupted")
        original_copy(source, destination)

    monkeypatch.setattr(bridge, "_copy_source_contents", fail_after_one_entry)
    bridge.converge_external_agent_skill(context, host_home=client.parent)
    monkeypatch.setattr(bridge, "_copy_source_contents", original_copy)

    bridge.converge_external_agent_skill(context, host_home=client.parent)
    bridge.converge_external_agent_skill(context, host_home=client.parent)

    assert _target(client, tmp_path).is_dir()
    assert list((client / "skills").glob(f".{SKILL}.ava-*")) == []


def test_late_target_keeps_stage_and_previous_in_transaction_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "repo"
    source = _source(repo)
    client = _client_home(tmp_path)
    context = _context(repo, tmp_path)
    bridge.converge_external_agent_skill(context, host_home=client.parent)
    target = _target(client, tmp_path)
    (source / "SKILL.md").write_text("operator v2\n")
    outside = tmp_path / "late-owned-by-user"
    outside.mkdir()
    (outside / "SKILL.md").write_text("user target\n")
    original_rename = bridge._rename_no_replace

    def insert_target(source_path: Path, destination: Path) -> None:
        if ".ava-stage-" in source_path.name and destination == target:
            destination.symlink_to(outside, target_is_directory=True)
        original_rename(source_path, destination)

    monkeypatch.setattr(bridge, "_rename_no_replace", insert_target)
    bridge.converge_external_agent_skill(context, host_home=client.parent)
    monkeypatch.setattr(bridge, "_rename_no_replace", original_rename)

    bridge.converge_external_agent_skill(context, host_home=client.parent)

    assert target.is_symlink()
    assert (outside / "SKILL.md").read_text() == "user target\n"
    ledger = _ledger(context)
    assert ledger["transaction"]["claim_state"] == "claimed"
    assert ledger["garbage"] == []
    assert {
        path.name.split(".ava-")[1].split("-")[0] for path in target.parent.glob(f".{SKILL}.ava-*")
    } == {
        "previous",
        "stage",
    }


def test_cleanup_retains_private_residue_without_path_unlink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "repo"
    source = _source(repo)
    client = _client_home(tmp_path)
    context = _context(repo, tmp_path)
    bridge.converge_external_agent_skill(context, host_home=client.parent)
    target = _target(client, tmp_path)
    (source / "SKILL.md").write_text("operator v2\n")
    original_unlink = Path.unlink
    unlink_attempts = 0

    def forbid_cleanup_unlink(path: Path, missing_ok: bool = False) -> None:
        nonlocal unlink_attempts
        if "-retained-previous-" in str(path):
            unlink_attempts += 1
            raise AssertionError(f"cleanup attempted pathname unlink: {path.name}")
        original_unlink(path, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", forbid_cleanup_unlink)
    bridge.converge_external_agent_skill(context, host_home=client.parent)
    assert unlink_attempts == 0
    ledger = json.loads(
        (context.ava_home / "configs" / "external-agent-skills" / "codex.json").read_text()
    )
    assert ledger["transaction"] is None
    assert ledger["garbage"] == []
    assert [item["kind"] for item in ledger["retained"]] == ["previous"]
    assert ledger["retained"][0]["location"] == "retained"

    bridge.converge_external_agent_skill(context, host_home=client.parent)

    assert (target / "SKILL.md").read_text() == "operator v2\n"
    assert list(target.parent.glob(f".{SKILL}.ava-*")) == []


def test_cleanup_rejects_same_content_hard_link_without_changing_outside_inode(
    tmp_path: Path,
) -> None:
    residue = tmp_path / "residue"
    residue.mkdir()
    residue_file = residue / "SKILL.md"
    residue_file.write_text("operator\n")
    residue_file.chmod(0o444)
    manifest = bridge_fs._tree_manifest(residue)
    residue_file.unlink()
    outside = tmp_path / "outside.md"
    outside.write_text("operator\n")
    outside.chmod(0o444)
    os.link(outside, residue_file)
    outside_before = outside.stat()

    with pytest.raises(bridge_fs._ClientConflictError, match="link"):
        bridge_fs._remove_manifest_subset(residue, manifest)

    assert residue_file.exists()
    assert outside.exists()
    assert residue_file.stat().st_ino == outside_before.st_ino
    assert stat.S_IMODE(outside.stat().st_mode) == stat.S_IMODE(outside_before.st_mode)
