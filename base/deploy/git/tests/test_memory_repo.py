from __future__ import annotations

import subprocess
from collections.abc import Sequence
from pathlib import Path

import pytest

from base.cluster.machine import MachineIdentity
from base.cluster.machine.tests.identity import machine_identity as machine_identity
from base.deploy.git import memory_repo
from base.deploy.git.tests.memory_checkouts import commit_note
from base.deploy.git.tests.memory_checkouts import memory_checkout as memory_checkout
from base.paths import gateway_memory_dir


def test_branch_name_gateway_tracks_main(*, machine_identity: MachineIdentity) -> None:
    machine_identity.set(role="gateway", name="cloud")
    assert memory_repo.branch_name(machine=machine_identity) == "main"


def test_branch_name_agent_runner_authors_machine_branch(
    *, machine_identity: MachineIdentity
) -> None:
    machine_identity.set(role="agent-runner", name="test-host-2")
    assert memory_repo.branch_name(machine=machine_identity) == "machine-test-host-2"


def test_branch_name_combined_unit_returns_machine_branch(
    *, machine_identity: MachineIdentity
) -> None:
    machine_identity.set(role="gateway,agent-runner", name="test-host")
    assert memory_repo.branch_name(machine=machine_identity) == "machine-test-host"


def test_pull_main_git_sequence_and_returned_sha(memory_checkout: tuple[Path, Path]) -> None:
    source, root = memory_checkout
    memory_repo.run_git("checkout", "-b", "consolidated", cwd=source)
    expected = commit_note(source, "consolidated update")
    memory_repo.run_git("push", "origin", "consolidated", cwd=source)

    sha = memory_repo.pull_main(root=root, keep_local=False, branch="consolidated")

    assert sha == expected
    assert memory_repo.run_git("rev-parse", "HEAD", cwd=root) == expected
    assert "consolidated update" in (root / "note.md").read_text()


def test_pull_main_rejects_divergence(memory_checkout: tuple[Path, Path]) -> None:
    source, root = memory_checkout
    commit_note(source, "remote change")
    memory_repo.run_git("push", "origin", "main", cwd=source)
    local = commit_note(root, "unpublished local change")

    with pytest.raises(subprocess.CalledProcessError):
        memory_repo.pull_main(root=root, keep_local=False, branch="main")

    assert memory_repo.run_git("rev-parse", "HEAD", cwd=root) == local
    assert "unpublished local change" in (root / "note.md").read_text()


def test_init_gateway_on_stale_machine_branch_switches_to_main(
    monkeypatch: pytest.MonkeyPatch,
    *,
    machine_identity: MachineIdentity,
) -> None:
    machine_identity.set(role="gateway", name="cloud")
    monkeypatch.setattr(memory_repo, "is_initialized", lambda: True)

    calls: list[tuple[str, ...]] = []

    def _fake_run_git(*args: str, **kwargs: object) -> str:
        calls.append(args)
        if args == ("rev-parse", "--abbrev-ref", "HEAD"):
            return "machine-cloud"
        return ""

    monkeypatch.setattr(memory_repo, "run_git", _fake_run_git)

    memory_repo.init(machine=machine_identity)

    assert ("fetch", "origin", "main") in calls
    assert ("checkout", "main") in calls


def test_init_agent_runner_on_wrong_branch_raises(
    monkeypatch: pytest.MonkeyPatch, *, machine_identity: MachineIdentity
) -> None:
    machine_identity.set(role="agent-runner", name="test-host-2")
    monkeypatch.setattr(memory_repo, "is_initialized", lambda: True)

    def _fake_run_git(*args: str, **kwargs: object) -> str:
        if args == ("rev-parse", "--abbrev-ref", "HEAD"):
            return "main"
        return ""

    monkeypatch.setattr(memory_repo, "run_git", _fake_run_git)

    with pytest.raises(memory_repo.MemoryBranchMismatch):
        memory_repo.init(machine=machine_identity)


def test_init_gateway_on_main_is_noop(
    monkeypatch: pytest.MonkeyPatch, *, machine_identity: MachineIdentity
) -> None:
    machine_identity.set(role="gateway", name="cloud")
    monkeypatch.setattr(memory_repo, "is_initialized", lambda: True)

    calls: list[tuple[str, ...]] = []

    def _fake_run_git(*args: str, **kwargs: object) -> str:
        calls.append(args)
        if args == ("rev-parse", "--abbrev-ref", "HEAD"):
            return "main"
        return ""

    monkeypatch.setattr(memory_repo, "run_git", _fake_run_git)

    memory_repo.init(machine=machine_identity)

    assert calls == [("rev-parse", "--abbrev-ref", "HEAD")]


def test_gateway_memory_dir_combined_uses_subdirectory(
    monkeypatch: pytest.MonkeyPatch, *, machine_identity: MachineIdentity
) -> None:
    from base.paths import meiva_home

    machine_identity.set(role="gateway,agent-runner", name="test-host")
    gmd = gateway_memory_dir(roles=machine_identity.role())
    home = meiva_home()
    assert gmd == home / "gateway" / "memory"

    from base.paths import memory_dir

    assert gmd != memory_dir()


def test_gateway_memory_dir_gateway_only_equals_memory_dir(
    *, machine_identity: MachineIdentity
) -> None:
    from base.paths import meiva_home, memory_dir

    machine_identity.set(role="gateway", name="cloud")
    gmd = gateway_memory_dir(roles=machine_identity.role())
    assert gmd == memory_dir()
    assert gmd == meiva_home() / "memory"


def test_init_gateway_creates_checkout_on_main(
    monkeypatch: pytest.MonkeyPatch,
    *,
    machine_identity: MachineIdentity,
) -> None:
    machine_identity.set(role="gateway", name="cloud")

    def initialized(*, machine: MachineIdentity) -> bool:
        assert machine is machine_identity
        return False

    monkeypatch.setattr(memory_repo, "gateway_is_initialized", initialized)

    calls: list[tuple[str, ...]] = []

    def _fake_run_git(*args: str, **kwargs: object) -> str:
        calls.append(args)
        return ""

    monkeypatch.setattr(memory_repo, "run_git", _fake_run_git)

    monkeypatch.setattr(
        memory_repo,
        "memory_remote",
        lambda: (_ for _ in ()).throw(memory_repo.MemoryRemoteMissing("test")),
    )

    import subprocess as sp

    from base.paths import gateway_memory_dir as _gmd

    def _fake_run(*args: object, **kwargs: object) -> None:
        if isinstance(args[0], list) and args[0][:2] == ["git", "init"]:
            _gmd(roles=machine_identity.role()).mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(sp, "run", _fake_run)

    memory_repo.init_gateway(machine=machine_identity)

    assert ("add", ".gitignore") in calls
    assert ("commit", "-q", "-m", "init: local memory on main") in calls


def test_init_gateway_already_initialized_on_main_is_noop(
    monkeypatch: pytest.MonkeyPatch,
    *,
    machine_identity: MachineIdentity,
) -> None:
    machine_identity.set(role="gateway", name="cloud")

    def initialized(*, machine: MachineIdentity) -> bool:
        assert machine is machine_identity
        return True

    monkeypatch.setattr(memory_repo, "gateway_is_initialized", initialized)

    calls: list[tuple[str, ...]] = []

    def _fake_run_git(*args: str, **kwargs: object) -> str:
        calls.append(args)
        if args == ("rev-parse", "--abbrev-ref", "HEAD"):
            return "main"
        return ""

    monkeypatch.setattr(memory_repo, "run_git", _fake_run_git)

    memory_repo.init_gateway(machine=machine_identity)

    assert calls == [("rev-parse", "--abbrev-ref", "HEAD")]


def test_init_gateway_on_wrong_branch_auto_switches(
    monkeypatch: pytest.MonkeyPatch,
    *,
    machine_identity: MachineIdentity,
) -> None:
    machine_identity.set(role="gateway", name="cloud")

    def initialized(*, machine: MachineIdentity) -> bool:
        assert machine is machine_identity
        return True

    monkeypatch.setattr(memory_repo, "gateway_is_initialized", initialized)

    calls: list[tuple[str, ...]] = []

    def _fake_run_git(*args: str, **kwargs: object) -> str:
        calls.append(args)
        if args == ("rev-parse", "--abbrev-ref", "HEAD"):
            return "machine-cloud"
        return ""

    monkeypatch.setattr(memory_repo, "run_git", _fake_run_git)

    memory_repo.init_gateway(machine=machine_identity)

    assert ("fetch", "origin", "main") in calls
    assert ("checkout", "main") in calls


def test_init_combined_unit_does_not_auto_switch_agent_runner_checkout(
    monkeypatch: pytest.MonkeyPatch,
    *,
    machine_identity: MachineIdentity,
) -> None:
    machine_identity.set(role="gateway,agent-runner", name="test-host")
    monkeypatch.setattr(memory_repo, "is_initialized", lambda: True)

    def _fake_run_git(*args: str, **kwargs: object) -> str:
        if args == ("rev-parse", "--abbrev-ref", "HEAD"):
            return "main"
        return ""

    monkeypatch.setattr(memory_repo, "run_git", _fake_run_git)

    with pytest.raises(memory_repo.MemoryBranchMismatch):
        memory_repo.init(machine=machine_identity)


def test_pull_main_keep_local_is_noop(tmp_path: Path) -> None:
    root = tmp_path / "local"
    memory_repo.init_local_repo("main", root)
    expected = commit_note(root, "local memory")

    assert memory_repo.run_git("remote", cwd=root) == ""
    assert memory_repo.pull_main(root=root, keep_local=True, branch="absent") == expected
    assert (root / "note.md").read_text().endswith("local memory\n")


def test_init_keep_local_strips_remote_when_branch_correct(
    monkeypatch: pytest.MonkeyPatch,
    *,
    machine_identity: MachineIdentity,
) -> None:
    machine_identity.set(role="agent-runner", name="test-host-2")
    monkeypatch.setattr(memory_repo.settings.general, "memory_keep_local", True)
    monkeypatch.setattr(memory_repo, "is_initialized", lambda: True)

    calls: list[tuple[str, ...]] = []

    def _fake_run_git(*args: str, **kwargs: object) -> str:
        calls.append(args)
        if args == ("rev-parse", "--abbrev-ref", "HEAD"):
            return "machine-test-host-2"
        if args == ("remote",):
            return "origin"
        return ""

    monkeypatch.setattr(memory_repo, "run_git", _fake_run_git)

    memory_repo.init(machine=machine_identity)

    assert ("remote", "remove", "origin") in calls
    assert not any(c[:1] == ("fetch",) for c in calls)


def test_init_keep_local_fresh_repo_inits_without_remote(
    monkeypatch: pytest.MonkeyPatch,
    *,
    machine_identity: MachineIdentity,
) -> None:
    # meiva:note
    # The existing initialization test records list-shaped subprocess argv and verifies that a fresh keep-local checkout initializes locally without clone or remote access. Its narrow Pyright annotation covers the dynamically typed argv item obtained through the existing subprocess replacement.
    # meiva:link base/deploy/git/tests/docs/memory-repo-tests.meiva.okf.md
    machine_identity.set(role="agent-runner", name="test-host-2")
    monkeypatch.setattr(memory_repo.settings.general, "memory_keep_local", True)
    monkeypatch.setattr(memory_repo, "is_initialized", lambda: False)

    calls: list[tuple[str, ...]] = []

    def _fake_run_git(*args: str, **kwargs: object) -> str:
        calls.append(args)
        return ""

    monkeypatch.setattr(memory_repo, "run_git", _fake_run_git)

    import subprocess as sp

    from base.paths import memory_dir as _md

    run_cmds: list[list[str]] = []

    def _fake_run(*args: object, **kwargs: object) -> None:
        cmd = args[0]
        assert isinstance(cmd, list)
        # meiva:note
        # The recorder takes object-typed argv items, so this narrow ignore covers one dynamically typed list element.
        run_cmds.append(cmd)  # pyright: ignore[reportUnknownArgumentType]
        if cmd[:2] == ["git", "init"]:
            _md().mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(sp, "run", _fake_run)

    memory_repo.init(machine=machine_identity)

    assert any(c[:2] == ["git", "init"] for c in run_cmds)
    assert not any(c[:2] == ["git", "clone"] for c in run_cmds)
    assert ("commit", "-q", "-m", "init: local memory on machine-test-host-2") in calls


def test_init_gateway_keep_local_strips_remote(
    monkeypatch: pytest.MonkeyPatch,
    *,
    machine_identity: MachineIdentity,
) -> None:
    machine_identity.set(role="gateway", name="cloud")
    monkeypatch.setattr(memory_repo.settings.general, "memory_keep_local", True)

    def initialized(*, machine: MachineIdentity) -> bool:
        assert machine is machine_identity
        return True

    monkeypatch.setattr(memory_repo, "gateway_is_initialized", initialized)

    calls: list[tuple[str, ...]] = []

    def _fake_run_git(*args: str, **kwargs: object) -> str:
        calls.append(args)
        if args == ("rev-parse", "--abbrev-ref", "HEAD"):
            return "main"
        if args == ("remote",):
            return "origin"
        return ""

    monkeypatch.setattr(memory_repo, "run_git", _fake_run_git)

    memory_repo.init_gateway(machine=machine_identity)

    assert ("remote", "remove", "origin") in calls
    assert not any(c[:1] == ("fetch",) for c in calls)


def test_run_git_is_bounded_and_kills_the_tree_on_expiry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[dict[str, object]] = []

    def _fake_run_bounded(
        argv: Sequence[str], *, timeout: float, **_kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        seen.append({"argv": list(argv), "timeout": timeout})
        return subprocess.CompletedProcess(list(argv), 0, "abc123\n", "")

    monkeypatch.setattr(memory_repo, "run_bounded", _fake_run_bounded)

    assert memory_repo.run_git("rev-parse", "HEAD") == "abc123"
    assert seen[0]["argv"] == ["git", "rev-parse", "HEAD"]
    assert seen[0]["timeout"] == memory_repo.GIT_TIMEOUT_S
    assert isinstance(seen[0]["timeout"], float)


def test_run_git_raises_on_non_zero_exit(monkeypatch: pytest.MonkeyPatch) -> None:

    def _fails(argv: Sequence[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(list(argv), 1, "", "fatal: no remote")

    monkeypatch.setattr(memory_repo, "run_bounded", _fails)
    with pytest.raises(subprocess.CalledProcessError) as exc:
        memory_repo.run_git("fetch", "origin", "main")
    assert exc.value.returncode == 1
    assert exc.value.stderr == "fatal: no remote"


def test_run_git_propagates_the_timeout(monkeypatch: pytest.MonkeyPatch) -> None:

    def _expire(argv: Sequence[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(list(argv), memory_repo.GIT_TIMEOUT_S)

    monkeypatch.setattr(memory_repo, "run_bounded", _expire)
    with pytest.raises(subprocess.TimeoutExpired):
        memory_repo.run_git("fetch", "origin", "main")
