"""Atomic skill publication: unsafe paths refuse before writes; crashes recover."""

from __future__ import annotations

import json
import os
import stat
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from base.cluster.machine import MachineIdentity
from base.config import ConfigBoot
from base.telemetry import EventPipeline
from cli.commands.converge.spec import ConvergeCtx
from cli.commands.extensions import external_skills as bridge
from cli.commands.extensions.external_skill_host import cleanup as bridge_cleanup
from cli.commands.extensions.external_skill_host import filesystem as bridge_fs
from cli.tests.bootstrap.command_inputs import command_config_machine as command_config_machine
from cli.tests.bootstrap.command_inputs import command_runtime as command_runtime
from tests.factories.external_skills import client_home, skill_ctx, skill_source, target_path
from tests.path_scoped.cli_tests import operator_database as operator_database
from tests.path_scoped.cli_tests import operator_pipeline as operator_pipeline

SKILL = "operating-ava-cluster"


@pytest.fixture(autouse=True)
def single_operator_skill(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(bridge, "_SKILL_NAMES", ("operating-ava-cluster",))


def _world(
    tmp_path: Path,
    *,
    operator_database: Callable[[], Any],
    producer: Callable[[], EventPipeline],
    command_config_machine: tuple[ConfigBoot, MachineIdentity],
) -> tuple[Path, Path, ConvergeCtx]:
    config, machine = command_config_machine
    repo = tmp_path / "repo"
    source = repo / "ava_builtins" / "skills" / "platform" / SKILL
    source.mkdir(parents=True)
    (source / "SKILL.md").write_text("operator v1\n")
    host_home = tmp_path / "host-home"
    client = host_home / ".codex"
    client.mkdir(parents=True)
    ava_home = tmp_path / "ava-home"
    (ava_home / "configs").mkdir(parents=True)
    return (
        source,
        client,
        ConvergeCtx(
            repo=repo,
            ava_home=ava_home,
            roles=None,
            config=config,
            database_factory=operator_database,
            producer=producer,
            machine=machine,
        ),
    )


def _target(client: Path) -> Path:
    return client / "skills" / SKILL


def _ledger(context: ConvergeCtx) -> dict[str, Any]:
    value: object = json.loads(
        (context.ava_home / "configs" / "external-agent-skills" / "codex.json").read_text()
    )
    return cast(dict[str, Any], value)


def _residues(client: Path) -> list[Path]:
    return list((client / "skills").glob(f".{SKILL}.ava-*"))


def test_restart_reconciles_stage_published_before_post_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    operator_database: Callable[[], Any],
    operator_pipeline: Callable[[], EventPipeline],
    *,
    command_config_machine: tuple[ConfigBoot, MachineIdentity],
) -> None:
    _, client, context = _world(
        tmp_path,
        operator_database=operator_database,
        producer=operator_pipeline,
        command_config_machine=command_config_machine,
    )
    target = _target(client)
    original_rename = bridge.rename_no_replace
    interrupted = False

    def interrupt_after_stage_publish(source_path: Path, destination: Path) -> None:
        nonlocal interrupted
        original_rename(source_path, destination)
        if destination.name.startswith(f".{SKILL}.ava-stage-") and not interrupted:
            interrupted = True
            raise SystemExit("process killed after stage publication")

    monkeypatch.setattr(bridge, "rename_no_replace", interrupt_after_stage_publish)
    with pytest.raises(SystemExit, match="stage publication"):
        bridge.converge_external_agent_skill(context, host_home=client.parent)

    assert interrupted
    assert _ledger(context)["transaction"]["stage_state"] == "publishing"
    target.mkdir()
    (target / "user.txt").write_text("late user target\n")
    monkeypatch.setattr(bridge, "rename_no_replace", original_rename)

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
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    operator_database: Callable[[], Any],
    operator_pipeline: Callable[[], EventPipeline],
    *,
    command_config_machine: tuple[ConfigBoot, MachineIdentity],
) -> None:
    source, client, context = _world(
        tmp_path,
        operator_database=operator_database,
        producer=operator_pipeline,
        command_config_machine=command_config_machine,
    )
    bridge.converge_external_agent_skill(context, host_home=client.parent)
    target = _target(client)
    installed_before = _ledger(context)["installed"]
    source.joinpath("SKILL.md").write_text("operator v2\n")
    original_rename = bridge.rename_no_replace
    interrupted = False

    def interrupt_after_claim(source_path: Path, destination: Path) -> None:
        nonlocal interrupted
        original_rename(source_path, destination)
        if source_path == target and not interrupted:
            interrupted = True
            raise SystemExit("process killed after target claim")

    monkeypatch.setattr(bridge, "rename_no_replace", interrupt_after_claim)
    with pytest.raises(SystemExit, match="target claim"):
        bridge.converge_external_agent_skill(context, host_home=client.parent)

    assert interrupted
    assert _ledger(context)["transaction"]["claim_state"] == "claiming"
    target.mkdir()
    (target / "user.txt").write_text("late user target\n")
    monkeypatch.setattr(bridge, "rename_no_replace", original_rename)

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


def test_cleanup_root_claim_restores_tree_modified_before_private_isolation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    operator_database: Callable[[], Any],
    operator_pipeline: Callable[[], EventPipeline],
    *,
    command_config_machine: tuple[ConfigBoot, MachineIdentity],
) -> None:
    source, client, context = _world(
        tmp_path,
        operator_database=operator_database,
        producer=operator_pipeline,
        command_config_machine=command_config_machine,
    )
    bridge.converge_external_agent_skill(context, host_home=client.parent)
    source.joinpath("SKILL.md").write_text("operator v2\n")
    outside = tmp_path / "outside.md"
    outside.write_text("outside\n")
    outside.chmod(0o444)
    outside_before = outside.stat()
    original_rename = bridge.rename_no_replace
    injected = False

    def swap_before_cleanup_claim(source_path: Path, destination: Path) -> None:
        nonlocal injected
        if ".ava-previous-" in source_path.name and "-retained-previous-" in destination.name:
            victim = source_path / "SKILL.md"
            victim.unlink()
            victim.symlink_to(outside)
            injected = True
        original_rename(source_path, destination)

    monkeypatch.setattr(bridge, "rename_no_replace", swap_before_cleanup_claim)

    bridge.converge_external_agent_skill(context, host_home=client.parent)

    assert injected
    assert (_target(client) / "SKILL.md").read_text() == "operator v2\n"
    previous = next((client / "skills").glob(f".{SKILL}.ava-previous-*"))
    swapped = previous / "SKILL.md"
    assert swapped.is_symlink()
    assert outside.read_text() == "outside\n"
    assert stat.S_IMODE(outside.stat().st_mode) == stat.S_IMODE(outside_before.st_mode)
    assert _ledger(context)["garbage"][0]["location"] == "source"

    monkeypatch.setattr(bridge, "rename_no_replace", original_rename)
    swapped.unlink()
    swapped.write_text("operator v1\n")
    bridge.converge_external_agent_skill(context, host_home=client.parent)

    assert _residues(client) == []
    assert _ledger(context)["garbage"] == []
    assert len(_ledger(context)["retained"]) == 1


def test_restart_reconciles_cleanup_claim_before_post_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    operator_database: Callable[[], Any],
    operator_pipeline: Callable[[], EventPipeline],
    *,
    command_config_machine: tuple[ConfigBoot, MachineIdentity],
) -> None:
    source, client, context = _world(
        tmp_path,
        operator_database=operator_database,
        producer=operator_pipeline,
        command_config_machine=command_config_machine,
    )
    bridge.converge_external_agent_skill(context, host_home=client.parent)
    source.joinpath("SKILL.md").write_text("operator v2\n")
    original_rename = bridge.rename_no_replace
    interrupted = False

    def interrupt_after_cleanup_claim(source_path: Path, destination: Path) -> None:
        nonlocal interrupted
        original_rename(source_path, destination)
        if "-retained-previous-" in destination.name and not interrupted:
            interrupted = True
            raise SystemExit("process killed after cleanup claim")

    monkeypatch.setattr(bridge, "rename_no_replace", interrupt_after_cleanup_claim)
    with pytest.raises(SystemExit, match="cleanup claim"):
        bridge.converge_external_agent_skill(context, host_home=client.parent)

    assert interrupted
    assert _ledger(context)["garbage"][0]["location"] == "claiming"
    monkeypatch.setattr(bridge, "rename_no_replace", original_rename)
    bridge.converge_external_agent_skill(context, host_home=client.parent)

    assert (_target(client) / "SKILL.md").read_text() == "operator v2\n"
    assert _ledger(context)["garbage"] == []
    assert len(_ledger(context)["retained"]) == 1
    assert _residues(client) == []


def test_unsupported_cleanup_never_changes_directory_permissions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    residue = tmp_path / "residue"
    nested = residue / "nested"
    nested.mkdir(parents=True)
    nested.chmod(0o500)
    manifest = bridge_fs.tree_manifest(residue)
    outside = tmp_path / "outside"
    outside.mkdir()
    outside.chmod(0o755)
    outside_before = outside.stat()

    def chmod_forbidden(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("cleanup must not chmod by pathname or descriptor")

    monkeypatch.setattr(Path, "chmod", chmod_forbidden)
    monkeypatch.setattr(bridge_fs.os, "fchmod", chmod_forbidden)

    with pytest.raises(bridge_fs.ClientConflictError):
        bridge_fs.remove_manifest_subset(residue, manifest)

    assert nested.is_dir()
    assert stat.S_IMODE(nested.stat().st_mode) == 0o500
    assert stat.S_IMODE(outside.stat().st_mode) == stat.S_IMODE(outside_before.st_mode)


def test_restart_does_not_adopt_ambiguous_per_file_claim(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    operator_database: Callable[[], Any],
    operator_pipeline: Callable[[], EventPipeline],
    *,
    command_config_machine: tuple[ConfigBoot, MachineIdentity],
) -> None:
    source, client, context = _world(
        tmp_path,
        operator_database=operator_database,
        producer=operator_pipeline,
        command_config_machine=command_config_machine,
    )
    bridge.converge_external_agent_skill(context, host_home=client.parent)
    source.joinpath("SKILL.md").write_text("operator v2\n")
    original_rename = bridge.rename_no_replace
    interrupted = False

    def interrupt_after_file_claim(source_path: Path, destination: Path) -> None:
        nonlocal interrupted
        if ".ava-retained-" in destination.name and not interrupted:
            ledger = _ledger(context)
            claiming = [
                claim
                for claim in ledger["garbage"][0]["file_claims"]
                if claim["state"] == "claiming"
            ]
            assert len(claiming) == 1
            assert destination.name in claiming[0]["quarantine"]
        original_rename(source_path, destination)
        if ".ava-retained-" in destination.name and not interrupted:
            interrupted = True
            raise SystemExit("process killed after per-file claim")

    monkeypatch.setattr(bridge, "rename_no_replace", interrupt_after_file_claim)
    with pytest.raises(SystemExit, match="per-file claim"):
        bridge.converge_external_agent_skill(context, host_home=client.parent)

    ledger = _ledger(context)
    assert ledger["garbage"][0]["location"] == "quarantine"
    assert "claiming" in {claim["state"] for claim in ledger["garbage"][0]["file_claims"]}

    monkeypatch.setattr(bridge, "rename_no_replace", original_rename)
    bridge.converge_external_agent_skill(context, host_home=client.parent)

    ledger = _ledger(context)
    assert ledger["garbage"] == []
    assert len(ledger["retained"]) == 1
    assert all(claim["state"] == "retained" for claim in ledger["retained"][0]["file_claims"])
    assert (_target(client) / "SKILL.md").read_text() == "operator v2\n"


def test_preexisting_per_file_quarantine_is_preserved_without_adoption(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    operator_database: Callable[[], Any],
    operator_pipeline: Callable[[], EventPipeline],
    *,
    command_config_machine: tuple[ConfigBoot, MachineIdentity],
) -> None:
    source, client, context = _world(
        tmp_path,
        operator_database=operator_database,
        producer=operator_pipeline,
        command_config_machine=command_config_machine,
    )
    bridge.converge_external_agent_skill(context, host_home=client.parent)
    source.joinpath("SKILL.md").write_text("operator v2\n")
    original_preserve = bridge_cleanup.preserve_claimed_tree
    collision: Path | None = None

    def inject_collision_after_root_verification(
        ledger_path: Path,
        ledger: dict[str, Any],
        item: dict[str, Any],
        quarantine: Path,
        rename: Any,
    ) -> None:
        nonlocal collision
        skill_claim = next(claim for claim in item["file_claims"] if claim["path"] == "SKILL.md")
        candidate = quarantine / skill_claim["quarantine"]
        candidate.write_text("late user path\n")
        collision = candidate
        original_preserve(ledger_path, ledger, item, quarantine, rename)

    monkeypatch.setattr(
        bridge_cleanup, "preserve_claimed_tree", inject_collision_after_root_verification
    )
    bridge.converge_external_agent_skill(context, host_home=client.parent)

    assert collision is not None
    assert collision.read_text() == "late user path\n"
    retained = _ledger(context)["retained"]
    assert len(retained) == 1
    skill_claim = next(claim for claim in retained[0]["file_claims"] if claim["path"] == "SKILL.md")
    assert skill_claim["state"] == "retained"
    assert (collision.parent / "SKILL.md").read_text() == "operator v1\n"
    assert (_target(client) / "SKILL.md").read_text() == "operator v2\n"


def test_per_file_claim_failure_is_terminal_after_private_root_claim(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    operator_database: Callable[[], Any],
    operator_pipeline: Callable[[], EventPipeline],
    *,
    command_config_machine: tuple[ConfigBoot, MachineIdentity],
) -> None:
    source, client, context = _world(
        tmp_path,
        operator_database=operator_database,
        producer=operator_pipeline,
        command_config_machine=command_config_machine,
    )
    bridge.converge_external_agent_skill(context, host_home=client.parent)
    source.joinpath("SKILL.md").write_text("operator v2\n")
    original_rename = bridge.rename_no_replace
    attempts = 0

    def fail_file_claim(source_path: Path, destination: Path) -> None:
        nonlocal attempts
        if ".ava-retained-" in destination.name:
            attempts += 1
            raise PermissionError("private residue is not writable")
        original_rename(source_path, destination)

    monkeypatch.setattr(bridge, "rename_no_replace", fail_file_claim)
    bridge.converge_external_agent_skill(context, host_home=client.parent)
    assert attempts >= 1
    attempts_after_cleanup = attempts
    ledger = _ledger(context)
    assert ledger["garbage"] == []
    assert len(ledger["retained"]) == 1

    bridge.converge_external_agent_skill(context, host_home=client.parent)

    assert attempts == attempts_after_cleanup
    assert (_target(client) / "SKILL.md").read_text() == "operator v2\n"


def test_cleanup_preserves_late_replacement_after_final_file_verification(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    residue = tmp_path / "residue"
    residue.mkdir()
    victim = residue / "SKILL.md"
    victim.write_text("operator\n")
    manifest = bridge_fs.tree_manifest(residue)
    outside = tmp_path / "outside.md"
    outside.write_text("outside\n")
    outside.chmod(0o444)
    outside_before = outside.stat()
    original_verify = bridge_fs.verify_cleanup_file
    late_replacement: Path | None = None

    def swap_after_verification(path: Path, expected: dict[str, Any]) -> None:
        nonlocal late_replacement
        original_verify(path, expected)
        claimed = path.with_name(f"{path.name}.claimed")
        path.rename(claimed)
        path.symlink_to(outside)
        late_replacement = path

    monkeypatch.setattr(bridge_fs, "verify_cleanup_file", swap_after_verification)

    with pytest.raises(bridge_fs.ClientConflictError):
        bridge_fs.remove_manifest_subset(residue, manifest)

    assert late_replacement is not None
    assert late_replacement.is_symlink()
    assert outside.read_text() == "outside\n"
    assert stat.S_IMODE(outside.stat().st_mode) == stat.S_IMODE(outside_before.st_mode)


def test_converge_terminally_records_post_verification_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    operator_database: Callable[[], Any],
    operator_pipeline: Callable[[], EventPipeline],
    *,
    command_config_machine: tuple[ConfigBoot, MachineIdentity],
) -> None:
    source, client, context = _world(
        tmp_path,
        operator_database=operator_database,
        producer=operator_pipeline,
        command_config_machine=command_config_machine,
    )
    bridge.converge_external_agent_skill(context, host_home=client.parent)
    source.joinpath("SKILL.md").write_text("operator v2\n")
    outside = tmp_path / "outside.md"
    outside.write_text("outside\n")
    outside.chmod(0o444)
    outside_before = outside.stat()
    original_verify = bridge_cleanup.verify_cleanup_file
    late_replacement: Path | None = None

    def swap_after_verification(path: Path, expected: dict[str, Any]) -> None:
        nonlocal late_replacement
        original_verify(path, expected)
        if path.name.startswith(".SKILL.md.ava-retained-"):
            claimed = path.with_name(f"{path.name}.claimed")
            path.rename(claimed)
            path.symlink_to(outside)
            late_replacement = path

    monkeypatch.setattr(bridge_cleanup, "verify_cleanup_file", swap_after_verification)
    bridge.converge_external_agent_skill(context, host_home=client.parent)

    assert late_replacement is not None
    assert late_replacement.is_symlink()
    assert outside.read_text() == "outside\n"
    assert stat.S_IMODE(outside.stat().st_mode) == stat.S_IMODE(outside_before.st_mode)
    ledger = _ledger(context)
    assert ledger["garbage"] == []
    assert len(ledger["retained"]) == 1
    assert all(claim["state"] == "retained" for claim in ledger["retained"][0]["file_claims"])

    monkeypatch.setattr(bridge_cleanup, "verify_cleanup_file", original_verify)
    bridge.converge_external_agent_skill(context, host_home=client.parent)

    assert late_replacement.is_symlink()
    assert (_target(client) / "SKILL.md").read_text() == "operator v2\n"


@pytest.mark.parametrize("linked_component", ["client-home", "skills-root"])
def test_linked_external_roots_are_rejected_without_following(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    linked_component: str,
    operator_database: Callable[[], Any],
    operator_pipeline: Callable[[], EventPipeline],
    *,
    command_config_machine: tuple[ConfigBoot, MachineIdentity],
) -> None:
    repo = tmp_path / "repo"
    skill_source(repo)
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
    bridge.converge_external_agent_skill(
        skill_ctx(
            repo,
            tmp_path,
            operator_database=operator_database,
            producer=operator_pipeline,
            command_config_machine=command_config_machine,
        ),
        host_home=host_home,
    )

    assert not (outside / SKILL).exists()
    assert "Codex" in capsys.readouterr().err


def test_linked_host_home_is_rejected_without_inspection(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    operator_database: Callable[[], Any],
    operator_pipeline: Callable[[], EventPipeline],
    *,
    command_config_machine: tuple[ConfigBoot, MachineIdentity],
) -> None:
    repo = tmp_path / "repo"
    skill_source(repo)
    outside_home = tmp_path / "outside-home"
    (outside_home / ".codex").mkdir(parents=True)
    linked_home = tmp_path / "linked-home"
    linked_home.symlink_to(outside_home, target_is_directory=True)

    bridge.converge_external_agent_skill(
        skill_ctx(
            repo,
            tmp_path,
            operator_database=operator_database,
            producer=operator_pipeline,
            command_config_machine=command_config_machine,
        ),
        host_home=linked_home,
    )

    assert not (outside_home / ".codex" / "skills").exists()
    assert "host home" in capsys.readouterr().err


def test_source_tree_link_is_fatal_before_copy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    operator_database: Callable[[], Any],
    operator_pipeline: Callable[[], EventPipeline],
    *,
    command_config_machine: tuple[ConfigBoot, MachineIdentity],
) -> None:
    repo = tmp_path / "repo"
    source = skill_source(repo)
    outside = tmp_path / "outside.md"
    outside.write_text("outside\n")
    (source / "references" / "linked.md").symlink_to(outside)
    client = client_home(tmp_path)
    with pytest.raises(RuntimeError, match="source"):
        bridge.converge_external_agent_skill(
            skill_ctx(
                repo,
                tmp_path,
                operator_database=operator_database,
                producer=operator_pipeline,
                command_config_machine=command_config_machine,
            ),
            host_home=client.parent,
        )

    assert not target_path(client, tmp_path).exists()


def test_source_path_component_link_is_fatal_before_copy(
    tmp_path: Path,
    operator_database: Callable[[], Any],
    operator_pipeline: Callable[[], EventPipeline],
    *,
    command_config_machine: tuple[ConfigBoot, MachineIdentity],
) -> None:
    repo = tmp_path / "repo"
    actual_agents = tmp_path / "actual-agents"
    source = actual_agents / "skills" / SKILL
    source.mkdir(parents=True)
    (source / "SKILL.md").write_text("operator\n")
    repo.mkdir()
    (repo / "ava_builtins").symlink_to(actual_agents, target_is_directory=True)
    client = client_home(tmp_path)

    with pytest.raises(RuntimeError, match="source"):
        bridge.converge_external_agent_skill(
            skill_ctx(
                repo,
                tmp_path,
                operator_database=operator_database,
                producer=operator_pipeline,
                command_config_machine=command_config_machine,
            ),
            host_home=client.parent,
        )

    assert not target_path(client, tmp_path).exists()


def test_cleanup_retains_private_residue_without_path_unlink(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    operator_database: Callable[[], Any],
    operator_pipeline: Callable[[], EventPipeline],
    *,
    command_config_machine: tuple[ConfigBoot, MachineIdentity],
) -> None:
    repo = tmp_path / "repo"
    source = skill_source(repo)
    client = client_home(tmp_path)
    context = skill_ctx(
        repo,
        tmp_path,
        operator_database=operator_database,
        producer=operator_pipeline,
        command_config_machine=command_config_machine,
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
