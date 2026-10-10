from __future__ import annotations

import importlib
import json
import os
import stat
import threading
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from base.telemetry import EventPipeline
from cli.commands.extensions import external_skills as bridge
from cli.commands.extensions.external_skill_host import filesystem as bridge_fs
from tests.factories.external_skills import (
    SKILL,
    client_home,
    read_ledger,
    skill_ctx,
    skill_source,
    target_path,
)
from tests.path_scoped.cli_tests import operator_database as operator_database
from tests.path_scoped.cli_tests import operator_pipeline as operator_pipeline


@pytest.fixture(autouse=True)
def single_operator_skill(monkeypatch: pytest.MonkeyPatch) -> None:
    module = importlib.import_module("cli.commands.extensions.external_skills")
    monkeypatch.setattr(module, "_SKILL_NAMES", ("operating-ava-cluster",))


def test_source_change_after_snapshot_does_not_mix_generations(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    operator_database: Callable[[], Any],
    operator_pipeline: Callable[[], EventPipeline],
) -> None:
    repo = tmp_path / "repo"
    source = skill_source(repo)
    client = client_home(tmp_path)
    second_client = client.parent / ".claude"
    second_client.mkdir()
    original_stage = bridge._stage_copy
    changed = False

    def change_source_then_stage(*args: Any, **kwargs: Any) -> Path:
        nonlocal changed
        if not changed:
            changed = True
            (source / "SKILL.md").write_text("operator v2\n")
        return original_stage(*args, **kwargs)

    monkeypatch.setattr(bridge, "_stage_copy", change_source_then_stage)

    bridge.converge_external_agent_skill(
        skill_ctx(repo, tmp_path, operator_database=operator_database, producer=operator_pipeline),
        host_home=client.parent,
    )

    assert (source / "SKILL.md").read_text() == "operator v2\n"
    assert (target_path(client, tmp_path) / "SKILL.md").read_text() == "operator v1\n"
    assert (target_path(second_client, tmp_path) / "SKILL.md").read_text() == "operator v1\n"


def test_path_and_open_handle_metadata_variants_are_not_a_source_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source_file = tmp_path / "SKILL.md"
    source_file.write_text("operator\n")
    real_fstat = bridge_fs.os.fstat

    def fstat_with_platform_metadata(fd: int) -> SimpleNamespace:
        current = real_fstat(fd)
        return SimpleNamespace(
            st_ctime_ns=current.st_ctime_ns + 1,
            st_dev=current.st_dev,
            st_ino=current.st_ino,
            st_mode=current.st_mode,
            st_mtime_ns=current.st_mtime_ns,
            st_nlink=current.st_nlink,
            st_size=current.st_size,
        )

    monkeypatch.setattr(bridge_fs.os, "fstat", fstat_with_platform_metadata)

    data, mode = bridge_fs.read_regular(source_file, source=True)

    assert data == b"operator\n"
    assert mode == stat.S_IMODE(source_file.stat().st_mode)


def test_linked_target_is_preserved_without_following(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    operator_database: Callable[[], Any],
    operator_pipeline: Callable[[], EventPipeline],
) -> None:
    repo = tmp_path / "repo"
    skill_source(repo)
    client = client_home(tmp_path)
    outside = tmp_path / "outside-target"
    outside.mkdir()
    (outside / "SKILL.md").write_text("outside\n")
    target = target_path(client, tmp_path)
    target.parent.mkdir()
    target.symlink_to(outside, target_is_directory=True)

    bridge.converge_external_agent_skill(
        skill_ctx(repo, tmp_path, operator_database=operator_database, producer=operator_pipeline),
        host_home=client.parent,
    )

    assert (outside / "SKILL.md").read_text() == "outside\n"
    assert "unmanaged" in capsys.readouterr().err


def test_windows_reparse_attribute_is_rejected() -> None:
    current = SimpleNamespace(st_file_attributes=0x400)

    assert bridge_fs.attributes_reparse(cast(Any, current))


def test_late_edit_between_check_and_claim_is_restored_not_overwritten(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    operator_database: Callable[[], Any],
    operator_pipeline: Callable[[], EventPipeline],
) -> None:
    repo = tmp_path / "repo"
    source = skill_source(repo)
    client = client_home(tmp_path)
    context = skill_ctx(
        repo, tmp_path, operator_database=operator_database, producer=operator_pipeline
    )
    bridge.converge_external_agent_skill(context, host_home=client.parent)
    target = target_path(client, tmp_path)
    (source / "SKILL.md").write_text("operator v2\n")
    original_stage = bridge._stage_copy

    def edit_after_check(
        snapshot: Any,
        source_manifest: list[dict[str, Any]],
        skills_root: Path,
        ledger_path: Path,
        ledger: dict[str, Any],
        source_digest: str,
        *,
        skill_name: str = SKILL,
    ) -> Path:
        staged = original_stage(
            snapshot,
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
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    operator_database: Callable[[], Any],
    operator_pipeline: Callable[[], EventPipeline],
) -> None:
    repo = tmp_path / "repo"
    source = skill_source(repo)
    client = client_home(tmp_path)
    context = skill_ctx(
        repo, tmp_path, operator_database=operator_database, producer=operator_pipeline
    )
    bridge.converge_external_agent_skill(context, host_home=client.parent)
    target = target_path(client, tmp_path)
    (source / "SKILL.md").write_text("operator v2\n")
    outside = tmp_path / "late-target"
    outside.mkdir()
    (outside / "SKILL.md").write_text("late user target\n")
    original_rename = bridge.rename_no_replace

    def insert_target_then_rename(source_path: Path, destination: Path) -> None:
        if ".ava-stage-" in source_path.name and destination == target:
            destination.symlink_to(outside, target_is_directory=True)
        original_rename(source_path, destination)

    monkeypatch.setattr(bridge, "rename_no_replace", insert_target_then_rename)

    bridge.converge_external_agent_skill(context, host_home=client.parent)
    assert target.is_symlink()
    assert (outside / "SKILL.md").read_text() == "late user target\n"
    assert "conflict" in capsys.readouterr().err


def test_late_previous_destination_during_claim_is_not_replaced_or_owned(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    operator_database: Callable[[], Any],
    operator_pipeline: Callable[[], EventPipeline],
) -> None:
    repo = tmp_path / "repo"
    source = skill_source(repo)
    client = client_home(tmp_path)
    context = skill_ctx(
        repo, tmp_path, operator_database=operator_database, producer=operator_pipeline
    )
    bridge.converge_external_agent_skill(context, host_home=client.parent)
    target = target_path(client, tmp_path)
    installed_before = read_ledger(context)["installed"]
    (source / "SKILL.md").write_text("operator v2\n")
    original_rename = bridge.rename_no_replace
    collision: Path | None = None

    def collide_with_claim(source_path: Path, destination: Path) -> None:
        nonlocal collision
        if source_path == target:
            collision = destination
            destination.mkdir()
            (destination / "user.txt").write_text("not Ava owned\n")
        original_rename(source_path, destination)

    monkeypatch.setattr(bridge, "rename_no_replace", collide_with_claim)

    bridge.converge_external_agent_skill(context, host_home=client.parent)
    assert collision is not None
    assert (collision / "user.txt").read_text() == "not Ava owned\n"
    assert (target / "SKILL.md").read_text() == "operator v1\n"
    ledger = read_ledger(context)
    assert ledger["installed"] == installed_before
    assert ledger["transaction"]["claim_state"] == "claiming"
    assert ledger["garbage"] == []


def test_late_target_during_verification_restore_is_not_replaced_or_disowned(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    operator_database: Callable[[], Any],
    operator_pipeline: Callable[[], EventPipeline],
) -> None:
    repo = tmp_path / "repo"
    source = skill_source(repo)
    client = client_home(tmp_path)
    context = skill_ctx(
        repo, tmp_path, operator_database=operator_database, producer=operator_pipeline
    )
    bridge.converge_external_agent_skill(context, host_home=client.parent)
    target = target_path(client, tmp_path)
    installed_before = read_ledger(context)["installed"]
    (source / "SKILL.md").write_text("operator v2\n")
    original_verify = bridge._verify_marker
    original_rename = bridge.rename_no_replace
    late_target = False

    def reject_claimed_previous(
        root: Path, installation_id: str, generation_id: str, *, skill_name: str = SKILL
    ) -> None:
        if ".ava-previous-" in root.name:
            raise bridge.ClientConflictError("claimed copy changed")
        original_verify(root, installation_id, generation_id, skill_name=skill_name)

    def collide_with_restore(source_path: Path, destination: Path) -> None:
        nonlocal late_target
        if ".ava-previous-" in source_path.name and destination == target:
            late_target = True
            destination.mkdir()
            (destination / "user.txt").write_text("late user target\n")
        original_rename(source_path, destination)

    monkeypatch.setattr(bridge, "_verify_marker", reject_claimed_previous)
    monkeypatch.setattr(bridge, "rename_no_replace", collide_with_restore)

    bridge.converge_external_agent_skill(context, host_home=client.parent)
    assert late_target
    assert (target / "user.txt").read_text() == "late user target\n"
    ledger = read_ledger(context)
    assert ledger["installed"] == installed_before
    assert ledger["transaction"]["claim_state"] == "claimed"
    assert ledger["garbage"] == []


def test_late_target_during_activation_restore_is_not_replaced_or_disowned(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    operator_database: Callable[[], Any],
    operator_pipeline: Callable[[], EventPipeline],
) -> None:
    repo = tmp_path / "repo"
    source = skill_source(repo)
    client = client_home(tmp_path)
    context = skill_ctx(
        repo, tmp_path, operator_database=operator_database, producer=operator_pipeline
    )
    bridge.converge_external_agent_skill(context, host_home=client.parent)
    target = target_path(client, tmp_path)
    installed_before = read_ledger(context)["installed"]
    (source / "SKILL.md").write_text("operator v2\n")
    original_rename = bridge.rename_no_replace
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

    monkeypatch.setattr(bridge, "rename_no_replace", fail_activation_then_collide_with_restore)

    bridge.converge_external_agent_skill(context, host_home=client.parent)
    assert late_target
    assert (target / "user.txt").read_text() == "late user target\n"
    ledger = read_ledger(context)
    assert ledger["installed"] == installed_before
    assert ledger["transaction"]["claim_state"] == "claimed"
    assert ledger["garbage"] == []


def test_preexisting_generation_sibling_is_preserved_without_false_ownership(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    operator_database: Callable[[], Any],
    operator_pipeline: Callable[[], EventPipeline],
) -> None:
    repo = tmp_path / "repo"
    source = skill_source(repo)
    client = client_home(tmp_path)
    context = skill_ctx(
        repo, tmp_path, operator_database=operator_database, producer=operator_pipeline
    )
    bridge.converge_external_agent_skill(context, host_home=client.parent)
    target = target_path(client, tmp_path)
    installed_before = read_ledger(context)["installed"]
    (source / "SKILL.md").write_text("operator v2\n")
    generation_id = "a" * 32
    sibling = target.parent / f".{SKILL}.ava-stage-{generation_id}"
    sibling.mkdir()
    (sibling / "user.txt").write_text("not Ava owned\n")
    monkeypatch.setattr(bridge.uuid, "uuid4", lambda: SimpleNamespace(hex=generation_id))

    bridge.converge_external_agent_skill(context, host_home=client.parent)
    assert (sibling / "user.txt").read_text() == "not Ava owned\n"
    assert (target / "SKILL.md").read_text() == "operator v1\n"
    ledger = read_ledger(context)
    assert ledger["installed"] == installed_before
    assert ledger["transaction"]["stage_state"] == "publishing"
    assert ledger["garbage"] == []


def test_concurrent_converges_serialize_transaction_owned_absence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    operator_database: Callable[[], Any],
    operator_pipeline: Callable[[], EventPipeline],
) -> None:
    repo = tmp_path / "repo"
    source = skill_source(repo)
    client = client_home(tmp_path)
    context = skill_ctx(
        repo, tmp_path, operator_database=operator_database, producer=operator_pipeline
    )
    bridge.converge_external_agent_skill(context, host_home=client.parent)
    target = target_path(client, tmp_path)
    (source / "SKILL.md").write_text("operator v2\n")
    claimed = threading.Event()
    release = threading.Event()
    second_activated_during_claim = threading.Event()
    original_rename = bridge.rename_no_replace
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

    monkeypatch.setattr(bridge, "rename_no_replace", pause_after_claim)
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
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    operator_database: Callable[[], Any],
    operator_pipeline: Callable[[], EventPipeline],
) -> None:
    repo = tmp_path / "repo"
    skill_source(repo, "repo operator\n")
    client = client_home(tmp_path)
    target = target_path(client, tmp_path)
    target.mkdir(parents=True)
    (target / "SKILL.md").write_text("spoofed user skill\n")
    digest = bridge.tree_digest(target)
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

    bridge.converge_external_agent_skill(
        skill_ctx(repo, tmp_path, operator_database=operator_database, producer=operator_pipeline),
        host_home=client.parent,
    )

    assert (target / "SKILL.md").read_text() == "spoofed user skill\n"
    assert "unmanaged" in capsys.readouterr().err


@pytest.mark.skipif(not hasattr(Path, "chmod"), reason="filesystem mode support required")
def test_permission_only_modification_is_a_conflict(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    operator_database: Callable[[], Any],
    operator_pipeline: Callable[[], EventPipeline],
) -> None:
    repo = tmp_path / "repo"
    source = skill_source(repo)
    client = client_home(tmp_path)
    context = skill_ctx(
        repo, tmp_path, operator_database=operator_database, producer=operator_pipeline
    )
    bridge.converge_external_agent_skill(context, host_home=client.parent)
    target = target_path(client, tmp_path)
    skill_file = target / "SKILL.md"
    changed_mode = 0o600 if stat.S_IMODE(skill_file.stat().st_mode) != 0o600 else 0o644
    skill_file.chmod(changed_mode)
    (source / "SKILL.md").write_text("operator v2\n")

    bridge.converge_external_agent_skill(context, host_home=client.parent)
    assert (target / "SKILL.md").read_text() == "operator v1\n"
    assert stat.S_IMODE(skill_file.stat().st_mode) == changed_mode
    assert "conflict" in capsys.readouterr().err


def test_source_modes_are_materialized_and_recorded(
    tmp_path: Path,
    operator_database: Callable[[], Any],
    operator_pipeline: Callable[[], EventPipeline],
) -> None:
    repo = tmp_path / "repo"
    source = skill_source(repo)
    references = source / "references"
    recovery = references / "recovery.md"
    references.chmod(0o555)
    recovery.chmod(0o444)
    client = client_home(tmp_path)
    context = skill_ctx(
        repo, tmp_path, operator_database=operator_database, producer=operator_pipeline
    )
    bridge.converge_external_agent_skill(context, host_home=client.parent)

    target = target_path(client, tmp_path)
    assert stat.S_IMODE((target / "references").stat().st_mode) == 0o555
    assert stat.S_IMODE((target / "references" / "recovery.md").stat().st_mode) == 0o444
    (source / "SKILL.md").write_text("operator v2\n")

    bridge.converge_external_agent_skill(context, host_home=client.parent)
    assert (target / "SKILL.md").read_text() == "operator v2\n"
    assert list(target.parent.glob(f".{SKILL}.ava-*")) == []


def test_external_filesystem_failure_is_label_only_and_fail_soft(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    operator_database: Callable[[], Any],
    operator_pipeline: Callable[[], EventPipeline],
) -> None:
    repo = tmp_path / "repo"
    skill_source(repo)
    codex = client_home(tmp_path)
    claude = codex.parent / ".claude"
    claude.mkdir()
    original_stage = bridge._stage_copy

    def inaccessible(
        snapshot: Any,
        source_manifest: list[dict[str, Any]],
        skills_root: Path,
        ledger_path: Path,
        ledger: dict[str, Any],
        source_digest: str,
        *,
        skill_name: str = SKILL,
    ) -> Path:
        if skills_root.parent.name == ".codex":
            raise PermissionError("/secret/absolute/client/path")
        return original_stage(
            snapshot,
            source_manifest,
            skills_root,
            ledger_path,
            ledger,
            source_digest,
            skill_name=skill_name,
        )

    monkeypatch.setattr(bridge, "_stage_copy", inaccessible)

    bridge.converge_external_agent_skill(
        skill_ctx(repo, tmp_path, operator_database=operator_database, producer=operator_pipeline),
        host_home=codex.parent,
    )

    output = capsys.readouterr()
    assert "Codex" in output.err
    assert "PermissionError" in output.err
    assert target_path(claude, tmp_path).is_dir()
    assert str(codex.parent) not in output.err + output.out


def test_cleanup_failure_after_activation_is_retried_without_rollback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    operator_database: Callable[[], Any],
    operator_pipeline: Callable[[], EventPipeline],
) -> None:
    repo = tmp_path / "repo"
    source = skill_source(repo)
    client = client_home(tmp_path)
    context = skill_ctx(
        repo, tmp_path, operator_database=operator_database, producer=operator_pipeline
    )
    bridge.converge_external_agent_skill(context, host_home=client.parent)
    target = target_path(client, tmp_path)
    (source / "SKILL.md").write_text("operator v2\n")
    original_rename = bridge.rename_no_replace
    failed = False

    def fail_previous_once(source_path: Path, destination: Path) -> None:
        nonlocal failed
        if f".{SKILL}.ava-previous-" in source_path.name and not failed:
            failed = True
            raise PermissionError("cleanup denied")
        original_rename(source_path, destination)

    monkeypatch.setattr(bridge, "rename_no_replace", fail_previous_once)
    bridge.converge_external_agent_skill(context, host_home=client.parent)
    monkeypatch.setattr(bridge, "rename_no_replace", original_rename)
    bridge.converge_external_agent_skill(context, host_home=client.parent)
    assert (target / "SKILL.md").read_text() == "operator v2\n"
    assert list(target.parent.glob(f".{SKILL}.ava-*")) == []
    output = capsys.readouterr()
    assert str(client.parent) not in output.err + output.out


def test_interrupted_post_activation_commit_recovers_deterministically(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    operator_database: Callable[[], Any],
    operator_pipeline: Callable[[], EventPipeline],
) -> None:
    repo = tmp_path / "repo"
    source = skill_source(repo)
    client = client_home(tmp_path)
    context = skill_ctx(
        repo, tmp_path, operator_database=operator_database, producer=operator_pipeline
    )
    bridge.converge_external_agent_skill(context, host_home=client.parent)
    target = target_path(client, tmp_path)
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
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    operator_database: Callable[[], Any],
    operator_pipeline: Callable[[], EventPipeline],
) -> None:
    repo = tmp_path / "repo"
    skill_source(repo)
    client = client_home(tmp_path)
    context = skill_ctx(
        repo, tmp_path, operator_database=operator_database, producer=operator_pipeline
    )
    original_materialize = bridge.materialize_source_snapshot
    failed = False

    def fail_after_one_entry(snapshot: Any, destination: Path) -> None:
        nonlocal failed
        if not failed:
            failed = True
            first = snapshot.files[0]
            bridge.write_new(destination / first.path, first.data, first.mode)
            raise PermissionError("copy interrupted")
        original_materialize(snapshot, destination)

    monkeypatch.setattr(bridge, "materialize_source_snapshot", fail_after_one_entry)
    bridge.converge_external_agent_skill(context, host_home=client.parent)
    monkeypatch.setattr(bridge, "materialize_source_snapshot", original_materialize)

    bridge.converge_external_agent_skill(context, host_home=client.parent)
    bridge.converge_external_agent_skill(context, host_home=client.parent)
    assert target_path(client, tmp_path).is_dir()
    assert list((client / "skills").glob(f".{SKILL}.ava-*")) == []


def test_late_target_keeps_stage_and_previous_in_transaction_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    operator_database: Callable[[], Any],
    operator_pipeline: Callable[[], EventPipeline],
) -> None:
    repo = tmp_path / "repo"
    source = skill_source(repo)
    client = client_home(tmp_path)
    context = skill_ctx(
        repo, tmp_path, operator_database=operator_database, producer=operator_pipeline
    )
    bridge.converge_external_agent_skill(context, host_home=client.parent)
    target = target_path(client, tmp_path)
    (source / "SKILL.md").write_text("operator v2\n")
    outside = tmp_path / "late-owned-by-user"
    outside.mkdir()
    (outside / "SKILL.md").write_text("user target\n")
    original_rename = bridge.rename_no_replace

    def insert_target(source_path: Path, destination: Path) -> None:
        if ".ava-stage-" in source_path.name and destination == target:
            destination.symlink_to(outside, target_is_directory=True)
        original_rename(source_path, destination)

    monkeypatch.setattr(bridge, "rename_no_replace", insert_target)
    bridge.converge_external_agent_skill(context, host_home=client.parent)
    monkeypatch.setattr(bridge, "rename_no_replace", original_rename)

    bridge.converge_external_agent_skill(context, host_home=client.parent)
    assert target.is_symlink()
    assert (outside / "SKILL.md").read_text() == "user target\n"
    ledger = read_ledger(context)
    assert ledger["transaction"]["claim_state"] == "claimed"
    assert ledger["garbage"] == []
    assert {
        path.name.split(".ava-")[1].split("-")[0] for path in target.parent.glob(f".{SKILL}.ava-*")
    } == {
        "previous",
        "stage",
    }


def test_cleanup_retains_private_residue_without_path_unlink(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    operator_database: Callable[[], Any],
    operator_pipeline: Callable[[], EventPipeline],
) -> None:
    repo = tmp_path / "repo"
    source = skill_source(repo)
    client = client_home(tmp_path)
    context = skill_ctx(
        repo, tmp_path, operator_database=operator_database, producer=operator_pipeline
    )
    bridge.converge_external_agent_skill(context, host_home=client.parent)
    target = target_path(client, tmp_path)
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
    manifest = bridge_fs.tree_manifest(residue)
    residue_file.unlink()
    outside = tmp_path / "outside.md"
    outside.write_text("operator\n")
    outside.chmod(0o444)
    os.link(outside, residue_file)
    outside_before = outside.stat()

    with pytest.raises(bridge_fs.ClientConflictError, match="link"):
        bridge_fs.remove_manifest_subset(residue, manifest)

    assert residue_file.exists()
    assert outside.exists()
    assert residue_file.stat().st_ino == outside_before.st_ino
    assert stat.S_IMODE(outside.stat().st_mode) == stat.S_IMODE(outside_before.st_mode)
