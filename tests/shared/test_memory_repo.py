"""`shared/memory_repo.py` unit tests — role-aware branch_name, pull_main git
argument sequence, and the gateway init() auto-switch onto main.

Identity is stubbed at its source via shared.machine.set_identity (the same
mechanism conftest's two-unit fixtures use), so every `from shared.machine
import machine_role` call site inside memory_repo sees the injected value.
These tests do not touch the network or a real remote: _run_git is recorded.
"""

from __future__ import annotations

import subprocess
from collections.abc import Generator, Sequence

import pytest

from shared import memory_repo
from shared.machine import reset_identity, set_identity
from shared.paths import gateway_memory_dir


@pytest.fixture(autouse=True)
def _identity_reset() -> Generator[None, None, None]:
    """Reset injected machine identity before and after each test so role/name
    injection never leaks across tests."""
    reset_identity()
    yield
    reset_identity()


def test_branch_name_gateway_tracks_main() -> None:
    set_identity(role="gateway", name="cloud")
    assert memory_repo.branch_name() == "main"


def test_branch_name_agent_runner_authors_machine_branch() -> None:
    set_identity(role="agent-runner", name="test-host-2")
    assert memory_repo.branch_name() == "machine-test-host-2"


def test_branch_name_combined_unit_returns_machine_branch() -> None:
    """On a combined unit (gateway+agent-runner), branch_name() returns the
    machine branch — the agent-runner capability takes precedence."""
    set_identity(role="gateway,agent-runner", name="test-host")
    assert memory_repo.branch_name() == "machine-test-host"


def test_pull_main_git_sequence_and_returned_sha(monkeypatch: pytest.MonkeyPatch) -> None:
    """pull_main fetches origin/main, fast-forward-merges it, then returns the
    rev-parse HEAD output verbatim. All git commands target the gateway memory
    path (gateway_memory_dir())."""
    calls: list[tuple[str, ...]] = []

    def _fake_run_git(*args: str, **kwargs: object) -> str:
        calls.append(args)
        if args == ("rev-parse", "HEAD"):
            return "deadbeef1234"
        return ""

    monkeypatch.setattr(memory_repo, "_run_git", _fake_run_git)

    sha = memory_repo.pull_main()

    assert sha == "deadbeef1234"
    assert calls == [
        ("fetch", "origin", "main"),
        ("merge", "--ff-only", "origin/main"),
        ("rev-parse", "HEAD"),
    ]


def test_init_gateway_on_stale_machine_branch_switches_to_main(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A pure gateway (no agent-runner) whose checkout was previously initialized
    on `machine-<name>` (before branch_name became role-aware) auto-switches onto
    main instead of raising — it never authors, so nothing is lost."""
    set_identity(role="gateway", name="cloud")
    monkeypatch.setattr(memory_repo, "is_initialized", lambda: True)

    calls: list[tuple[str, ...]] = []

    def _fake_run_git(*args: str, **kwargs: object) -> str:
        calls.append(args)
        if args == ("rev-parse", "--abbrev-ref", "HEAD"):
            return "machine-cloud"  # stale branch from a prior role
        return ""

    monkeypatch.setattr(memory_repo, "_run_git", _fake_run_git)

    memory_repo.init()  # must not raise

    assert ("fetch", "origin", "main") in calls
    assert ("checkout", "main") in calls


def test_init_agent_runner_on_wrong_branch_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """A runner stays strict: an unexpected branch may carry unpushed in-flight
    work, so init() refuses to unilaterally switch."""
    set_identity(role="agent-runner", name="test-host-2")
    monkeypatch.setattr(memory_repo, "is_initialized", lambda: True)

    def _fake_run_git(*args: str, **kwargs: object) -> str:
        if args == ("rev-parse", "--abbrev-ref", "HEAD"):
            return "main"  # not machine-test-host-2
        return ""

    monkeypatch.setattr(memory_repo, "_run_git", _fake_run_git)

    with pytest.raises(memory_repo.MemoryBranchMismatch):
        memory_repo.init()


def test_init_gateway_on_main_is_noop(monkeypatch: pytest.MonkeyPatch) -> None:
    """When the gateway-only checkout is already on main, init() returns without
    issuing any fetch/checkout."""
    set_identity(role="gateway", name="cloud")
    monkeypatch.setattr(memory_repo, "is_initialized", lambda: True)

    calls: list[tuple[str, ...]] = []

    def _fake_run_git(*args: str, **kwargs: object) -> str:
        calls.append(args)
        if args == ("rev-parse", "--abbrev-ref", "HEAD"):
            return "main"
        return ""

    monkeypatch.setattr(memory_repo, "_run_git", _fake_run_git)

    memory_repo.init()

    assert calls == [("rev-parse", "--abbrev-ref", "HEAD")]


# ── gateway_memory_dir() path tests ──


def test_gateway_memory_dir_combined_uses_subdirectory(monkeypatch: pytest.MonkeyPatch) -> None:
    """On a combined unit, gateway_memory_dir() returns
    $AVA_HOME/gateway/memory, separate from memory_dir()."""
    from shared.paths import ava_home

    set_identity(role="gateway,agent-runner", name="test-host")
    gmd = gateway_memory_dir()
    home = ava_home()
    assert gmd == home / "gateway" / "memory"
    # Must differ from the agent-runner path
    from shared.paths import memory_dir

    assert gmd != memory_dir()


def test_gateway_memory_dir_gateway_only_equals_memory_dir() -> None:
    """On a gateway-only unit, gateway_memory_dir() is the same as memory_dir()."""
    from shared.paths import ava_home, memory_dir

    set_identity(role="gateway", name="cloud")
    gmd = gateway_memory_dir()
    assert gmd == memory_dir()
    assert gmd == ava_home() / "memory"


# ── init_gateway() tests ──


def test_init_gateway_creates_checkout_on_main(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """init_gateway() sets up a git repo on `main` at the gateway memory path."""
    set_identity(role="gateway", name="cloud")
    monkeypatch.setattr(memory_repo, "gateway_is_initialized", lambda: False)

    calls: list[tuple[str, ...]] = []

    def _fake_run_git(*args: str, **kwargs: object) -> str:
        calls.append(args)
        return ""

    monkeypatch.setattr(memory_repo, "_run_git", _fake_run_git)
    # Go through the no-remote path: raise MemoryRemoteMissing so init_gateway()
    # creates a local empty repo.
    monkeypatch.setattr(
        memory_repo,
        "memory_remote",
        lambda: (_ for _ in ()).throw(memory_repo.MemoryRemoteMissing("test")),
    )
    # Stub subprocess.run for git init — must create the directory so
    # .gitignore write succeeds (git init would normally create it).
    import subprocess as sp

    from shared.paths import gateway_memory_dir as _gmd

    def _fake_run(*args: object, **kwargs: object) -> None:
        if isinstance(args[0], list) and args[0][:2] == ["git", "init"]:
            _gmd().mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(sp, "run", _fake_run)

    memory_repo.init_gateway()

    # Should have done the local init flow: add .gitignore + commit
    assert ("add", ".gitignore") in calls
    assert ("commit", "-q", "-m", "init: local memory on main") in calls


def test_init_gateway_already_initialized_on_main_is_noop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the gateway checkout is already on main, init_gateway() is a noop."""
    set_identity(role="gateway", name="cloud")
    monkeypatch.setattr(memory_repo, "gateway_is_initialized", lambda: True)

    calls: list[tuple[str, ...]] = []

    def _fake_run_git(*args: str, **kwargs: object) -> str:
        calls.append(args)
        if args == ("rev-parse", "--abbrev-ref", "HEAD"):
            return "main"
        return ""

    monkeypatch.setattr(memory_repo, "_run_git", _fake_run_git)

    memory_repo.init_gateway()

    assert calls == [("rev-parse", "--abbrev-ref", "HEAD")]


def test_init_gateway_on_wrong_branch_auto_switches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Gateway checkout on a stale branch auto-switches to main."""
    set_identity(role="gateway", name="cloud")
    monkeypatch.setattr(memory_repo, "gateway_is_initialized", lambda: True)

    calls: list[tuple[str, ...]] = []

    def _fake_run_git(*args: str, **kwargs: object) -> str:
        calls.append(args)
        if args == ("rev-parse", "--abbrev-ref", "HEAD"):
            return "machine-cloud"  # stale
        return ""

    monkeypatch.setattr(memory_repo, "_run_git", _fake_run_git)

    memory_repo.init_gateway()

    assert ("fetch", "origin", "main") in calls
    assert ("checkout", "main") in calls


def test_init_combined_unit_does_not_auto_switch_agent_runner_checkout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """On a combined unit, init() for the agent-runner path raises
    MemoryBranchMismatch on a wrong branch instead of auto-switching
    (the gateway capability no longer overrides agent-runner safety)."""
    set_identity(role="gateway,agent-runner", name="test-host")
    monkeypatch.setattr(memory_repo, "is_initialized", lambda: True)

    def _fake_run_git(*args: str, **kwargs: object) -> str:
        if args == ("rev-parse", "--abbrev-ref", "HEAD"):
            return "main"  # not machine-test-host
        return ""

    monkeypatch.setattr(memory_repo, "_run_git", _fake_run_git)

    with pytest.raises(memory_repo.MemoryBranchMismatch):
        memory_repo.init()


# ── AVA_MEMORY_KEEP_LOCAL (local-only pool) ──


def test_pull_main_keep_local_is_noop(monkeypatch: pytest.MonkeyPatch) -> None:
    """With AVA_MEMORY_KEEP_LOCAL set, pull_main() does not fetch/merge (there is
    no remote) — it just reports the current HEAD."""
    set_identity(role="gateway,agent-runner", name="test-host-2")
    monkeypatch.setattr(memory_repo.settings.general, "memory_keep_local", True)

    calls: list[tuple[str, ...]] = []

    def _fake_run_git(*args: str, **kwargs: object) -> str:
        calls.append(args)
        if args == ("rev-parse", "HEAD"):
            return "cafef00d"
        return ""

    monkeypatch.setattr(memory_repo, "_run_git", _fake_run_git)

    sha = memory_repo.pull_main()

    assert sha == "cafef00d"
    assert calls == [("rev-parse", "HEAD")]


def test_init_keep_local_strips_remote_when_branch_correct(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A runner in keep-local mode whose checkout is already on the right branch
    drops any leftover remote so it can never sync off-box, and never fetches."""
    set_identity(role="agent-runner", name="test-host-2")
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

    monkeypatch.setattr(memory_repo, "_run_git", _fake_run_git)

    memory_repo.init()

    assert ("remote", "remove", "origin") in calls
    assert not any(c[:1] == ("fetch",) for c in calls)


def test_init_keep_local_fresh_repo_inits_without_remote(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A fresh keep-local checkout is `git init`-ed locally with no clone."""
    set_identity(role="agent-runner", name="test-host-2")
    monkeypatch.setattr(memory_repo.settings.general, "memory_keep_local", True)
    monkeypatch.setattr(memory_repo, "is_initialized", lambda: False)

    calls: list[tuple[str, ...]] = []

    def _fake_run_git(*args: str, **kwargs: object) -> str:
        calls.append(args)
        return ""

    monkeypatch.setattr(memory_repo, "_run_git", _fake_run_git)

    import subprocess as sp

    from shared.paths import memory_dir as _md

    run_cmds: list[list[str]] = []

    def _fake_run(*args: object, **kwargs: object) -> None:
        cmd = args[0]
        assert isinstance(cmd, list)
        run_cmds.append(cmd)  # pyright: ignore[reportUnknownArgumentType]
        if cmd[:2] == ["git", "init"]:
            _md().mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(sp, "run", _fake_run)

    memory_repo.init()

    assert any(c[:2] == ["git", "init"] for c in run_cmds)
    assert not any(c[:2] == ["git", "clone"] for c in run_cmds)
    assert ("commit", "-q", "-m", "init: local memory on machine-test-host-2") in calls


def test_init_gateway_keep_local_strips_remote(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """init_gateway() in keep-local mode drops any leftover remote and never
    fetches."""
    set_identity(role="gateway", name="cloud")
    monkeypatch.setattr(memory_repo.settings.general, "memory_keep_local", True)
    monkeypatch.setattr(memory_repo, "gateway_is_initialized", lambda: True)

    calls: list[tuple[str, ...]] = []

    def _fake_run_git(*args: str, **kwargs: object) -> str:
        calls.append(args)
        if args == ("rev-parse", "--abbrev-ref", "HEAD"):
            return "main"
        if args == ("remote",):
            return "origin"
        return ""

    monkeypatch.setattr(memory_repo, "_run_git", _fake_run_git)

    memory_repo.init_gateway()

    assert ("remote", "remove", "origin") in calls
    assert not any(c[:1] == ("fetch",) for c in calls)


# ── _run_git is bounded ─────────────────────────────────────────────────────


def test_run_git_is_bounded_and_kills_the_tree_on_expiry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every git call carries a timeout, via `run_bounded` rather than a plain
    `subprocess.run(timeout=...)`.

    Unbounded, `pull_main`'s `git fetch` does not merely stall its caller: the
    memory indexer runs it through `asyncio.to_thread`, and `asyncio.run()`'s
    shutdown waits on `shutdown_default_executor()` — so a fetch wedged against a
    dead remote holds the daemon's exit open long past the SIGTERM it was told to
    stop on. `run_bounded` (not the raw kwarg) because on Windows the direct
    child is Git-for-Windows' launcher stub, and a plain timeout would kill the
    stub and leave the real git running.
    """
    seen: list[dict[str, object]] = []

    def _fake_run_bounded(
        argv: Sequence[str], *, timeout: float, **_kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        seen.append({"argv": list(argv), "timeout": timeout})
        return subprocess.CompletedProcess(list(argv), 0, "abc123\n", "")

    monkeypatch.setattr(memory_repo, "run_bounded", _fake_run_bounded)

    assert memory_repo._run_git("rev-parse", "HEAD") == "abc123"
    assert seen[0]["argv"] == ["git", "rev-parse", "HEAD"]
    assert seen[0]["timeout"] == memory_repo._GIT_TIMEOUT_S
    assert isinstance(seen[0]["timeout"], float)


def test_run_git_raises_on_non_zero_exit(monkeypatch: pytest.MonkeyPatch) -> None:
    """`run_bounded` deliberately has no `check=`, so the raise-on-non-zero half
    of `_run_git`'s contract lives in `_run_git` — a failed git must not read as
    empty output to callers that branch on it."""

    def _fails(argv: Sequence[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(list(argv), 1, "", "fatal: no remote")

    monkeypatch.setattr(memory_repo, "run_bounded", _fails)
    with pytest.raises(subprocess.CalledProcessError) as exc:
        memory_repo._run_git("fetch", "origin", "main")
    assert exc.value.returncode == 1
    assert exc.value.stderr == "fatal: no remote"


def test_run_git_propagates_the_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    """A tripped bound surfaces as TimeoutExpired rather than being swallowed
    into a success — the indexer's caller logs it and retries next cycle."""

    def _expire(argv: Sequence[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(list(argv), memory_repo._GIT_TIMEOUT_S)

    monkeypatch.setattr(memory_repo, "run_bounded", _expire)
    with pytest.raises(subprocess.TimeoutExpired):
        memory_repo._run_git("fetch", "origin", "main")
