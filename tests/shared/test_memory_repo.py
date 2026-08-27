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
from pathlib import Path
from typing import ClassVar

import pytest

from shared import memory_repo
from shared.config import settings
from shared.config.general import GeneralSettings
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


def test_status_last_fetch_renders_cluster_zone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """RepoStatus.last_fetch is a display timestamp: it renders in the cluster
    timezone (user ruling 2026-08-27), never the host OS zone."""

    import datetime as dt

    monkeypatch.setattr(
        settings, "general", GeneralSettings.model_construct(timezone="Asia/Shanghai")
    )
    monkeypatch.setattr(memory_repo, "is_initialized", lambda: True)
    monkeypatch.setattr(memory_repo, "memory_dir", lambda: tmp_path)

    def _fake_run_git(*args: str, **kwargs: object) -> str:
        argv = list(args)
        if argv[:2] == ["rev-parse", "--abbrev-ref"]:
            return "machine-main"
        if argv[:2] == ["status", "--porcelain"]:
            return ""
        if argv == ["rev-list", "--count", "HEAD"]:
            return "3"
        if argv[:3] == ["rev-list", "--left-right", "--count"]:
            return "1\t0"
        raise AssertionError(f"unexpected git call: {argv}")

    monkeypatch.setattr(memory_repo, "_run_git", _fake_run_git)
    fetch_head = tmp_path / ".git" / "FETCH_HEAD"
    fetch_head.parent.mkdir(parents=True)
    fetch_head.write_text("dummy\n")
    # A fixed mtime far from the assertion moment: 2026-01-03 09:05 UTC.
    fixed = dt.datetime(2026, 1, 3, 9, 5, tzinfo=dt.UTC).timestamp()
    import os

    os.utime(fetch_head, (fixed, fixed))

    status = memory_repo.status()
    assert status.last_fetch == "2026-01-03T17:05:00+08:00"


# ── gateway pool bootstrap (no-remote split runner) ──


def test_init_split_runner_without_remote_bootstraps_from_gateway(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A fresh split agent-runner with no memory remote fetches the shared pool
    from the gateway instead of silently birthing a stub local repo
    (2026-08-27 company-mini incident)."""
    set_identity(role="agent-runner", name="test-host-2")
    monkeypatch.setattr(memory_repo, "is_initialized", lambda: False)
    monkeypatch.setattr(
        memory_repo,
        "memory_remote",
        lambda: (_ for _ in ()).throw(memory_repo.MemoryRemoteMissing("test")),
    )

    bootstrapped: list[object] = []

    def _fake_bootstrap() -> None:
        bootstrapped.append(True)

    local_inits: list[tuple[str, Path]] = []

    def _fake_local_init(branch: str, cwd: Path) -> None:
        local_inits.append((branch, cwd))

    monkeypatch.setattr(memory_repo, "bootstrap_from_gateway", _fake_bootstrap)
    monkeypatch.setattr(memory_repo, "_init_local_repo", _fake_local_init)

    memory_repo.init()

    assert bootstrapped == [True]
    assert local_inits == []


def test_init_split_runner_bootstrap_failure_raises_loud(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the gateway fetch fails, a split runner fails loudly with guidance —
    never a silent stub pool."""
    set_identity(role="agent-runner", name="test-host-2")
    monkeypatch.setattr(memory_repo, "is_initialized", lambda: False)
    monkeypatch.setattr(
        memory_repo,
        "memory_remote",
        lambda: (_ for _ in ()).throw(memory_repo.MemoryRemoteMissing("test")),
    )

    def _failing_bootstrap() -> None:
        raise memory_repo.MemoryPoolBootstrapFailed("GET http://gw/api/memory/pool -> HTTP 502")

    monkeypatch.setattr(memory_repo, "bootstrap_from_gateway", _failing_bootstrap)

    with pytest.raises(memory_repo.MemoryRemoteMissing, match="gateway pool snapshot"):
        memory_repo.init()


def test_init_combined_unit_without_remote_still_local_inits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A gateway-capable unit with no remote keeps the silent local init — on
    first boot its own gateway half is not serving yet, so there is nothing to
    fetch from (single-box fresh install path preserved)."""
    set_identity(role="gateway,agent-runner", name="test-host")
    monkeypatch.setattr(memory_repo, "is_initialized", lambda: False)
    monkeypatch.setattr(
        memory_repo,
        "memory_remote",
        lambda: (_ for _ in ()).throw(memory_repo.MemoryRemoteMissing("test")),
    )

    bootstrapped: list[object] = []

    def _fake_bootstrap() -> None:
        bootstrapped.append(True)

    local_inits: list[tuple[str, Path]] = []

    def _fake_local_init(branch: str, cwd: Path) -> None:
        local_inits.append((branch, cwd))

    monkeypatch.setattr(memory_repo, "bootstrap_from_gateway", _fake_bootstrap)
    monkeypatch.setattr(memory_repo, "_init_local_repo", _fake_local_init)

    memory_repo.init()

    assert bootstrapped == []
    assert [b for b, _ in local_inits] == ["machine-test-host"]


def _make_real_bundle(source: Path) -> tuple[str, bytes]:
    """Build a real git repo at `source` with a note tree and return
    (head sha, bundle bytes) — the exact wire shape the gateway serves."""
    source.mkdir(parents=True, exist_ok=True)
    (source / "MEMORY.md").write_text("# shared index\n")
    (source / "health").mkdir()
    (source / "health" / "a.md").write_text("note")
    for cmd in (
        ["git", "init", "-q", "-b", "main"],
        ["git", "-c", "user.email=a@b", "-c", "user.name=t", "add", "-A"],
        ["git", "-c", "user.email=a@b", "-c", "user.name=t", "commit", "-q", "-m", "base"],
    ):
        subprocess.run(cmd, cwd=source, check=True, capture_output=True)  # noqa: S603
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=source, check=True, capture_output=True, text=True
    ).stdout.strip()
    bundle = source.parent / "pool.bundle"
    subprocess.run(  # noqa: S603
        ["git", "bundle", "create", str(bundle), "HEAD"],
        cwd=source,
        check=True,
        capture_output=True,
    )
    return head, bundle.read_bytes()


def test_bootstrap_from_gateway_clones_bundle_as_machine_branch(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """bootstrap_from_gateway clones the gateway-served git bundle into the pool
    dir on the machine branch — a true descendant of main, with the junk
    bundle-path origin removed (real git end to end)."""
    from shared.paths import memory_dir as _md

    head, bundle_bytes = _make_real_bundle(tmp_path / "src")

    captured: dict[str, object] = {}

    def _fake_download(url: str, headers: dict[str, str]) -> tuple[str | None, bytes]:
        captured["url"] = url
        captured["headers"] = headers
        return head, bundle_bytes

    monkeypatch.setattr(memory_repo, "_download_pool_snapshot", _fake_download)

    import shared.machine as _machine

    monkeypatch.setattr(_machine, "gateway_api_base", lambda: "http://gw.example:8000")
    monkeypatch.setattr(
        _machine, "gateway_auth_headers", lambda: {"Authorization": "Bearer s3cret"}
    )

    set_identity(role="agent-runner", name="test-host-2")
    memory_repo.bootstrap_from_gateway()

    assert captured["url"] == "http://gw.example:8000/api/memory/pool"
    assert captured["headers"] == {"Authorization": "Bearer s3cret"}
    pool = _md()
    assert (pool / "MEMORY.md").read_text() == "# shared index\n"
    assert (pool / "health" / "a.md").read_text() == "note"
    assert (pool / ".git").is_dir()
    branch = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"],
        cwd=pool,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert branch == "machine-test-host-2"
    remotes = subprocess.run(
        ["git", "remote"], cwd=pool, check=True, capture_output=True, text=True
    ).stdout.strip()
    assert remotes == ""
    pool_head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=pool, check=True, capture_output=True, text=True
    ).stdout.strip()
    assert pool_head == head


def test_bootstrap_from_gateway_head_mismatch_fails_loud(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """When the bundle's HEAD is not the advertised X-Pool-Head, bootstrap fails
    loud instead of installing a torn snapshot."""
    _head, bundle_bytes = _make_real_bundle(tmp_path / "src")

    def _fake_download(url: str, headers: dict[str, str]) -> tuple[str | None, bytes]:
        return "deadbeefdeadbeef", bundle_bytes

    monkeypatch.setattr(memory_repo, "_download_pool_snapshot", _fake_download)

    import shared.machine as _machine

    monkeypatch.setattr(_machine, "gateway_api_base", lambda: "http://gw.example:8000")

    set_identity(role="agent-runner", name="test-host-2")
    with pytest.raises(memory_repo.MemoryPoolBootstrapFailed, match="does not match"):
        memory_repo.bootstrap_from_gateway()


def test_bootstrap_from_gateway_non_200_fails_loud(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-200 gateway answer surfaces as MemoryPoolBootstrapFailed with the
    status in the message — the guided failure, not a raw traceback."""

    def _failing_download(url: str, headers: dict[str, str]) -> tuple[str | None, bytes]:
        raise memory_repo.MemoryPoolBootstrapFailed("GET http://gw/api/memory/pool -> HTTP 502")

    monkeypatch.setattr(memory_repo, "_download_pool_snapshot", _failing_download)

    import shared.machine as _machine

    monkeypatch.setattr(_machine, "gateway_api_base", lambda: "http://gw")
    set_identity(role="agent-runner", name="test-host-2")
    with pytest.raises(memory_repo.MemoryPoolBootstrapFailed, match="HTTP 502"):
        memory_repo.bootstrap_from_gateway()


def test_bootstrap_from_gateway_missing_gateway_url_fails_loud(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A runner with no gateway URL configured fails with guidance, not the
    raw GatewayApiBaseMissing traceback."""
    import shared.machine as _machine

    monkeypatch.setattr(
        _machine,
        "gateway_api_base",
        lambda: (_ for _ in ()).throw(
            _machine.GatewayApiBaseMissing("gateway_url unset — `ava enroll` writes it")
        ),
    )
    set_identity(role="agent-runner", name="test-host-2")
    with pytest.raises(memory_repo.MemoryPoolBootstrapFailed, match="gateway URL unavailable"):
        memory_repo.bootstrap_from_gateway()


def test_bootstrap_from_gateway_corrupt_bundle_fails_loud(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A body that is not a valid git bundle fails at clone time and is wrapped
    into MemoryPoolBootstrapFailed (real git clone of junk bytes)."""

    def _junk_download(url: str, headers: dict[str, str]) -> tuple[str | None, bytes]:
        return None, b"this is not a git bundle"

    monkeypatch.setattr(memory_repo, "_download_pool_snapshot", _junk_download)

    import shared.machine as _machine

    monkeypatch.setattr(_machine, "gateway_api_base", lambda: "http://gw.example:8000")
    set_identity(role="agent-runner", name="test-host-2")
    with pytest.raises(memory_repo.MemoryPoolBootstrapFailed, match="git clone from bundle failed"):
        memory_repo.bootstrap_from_gateway()


def test_download_pool_snapshot_streams_and_enforces_cap_midstream(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """_download_pool_snapshot streams the body and aborts the moment the cap is
    crossed — it does not buffer past _POOL_SNAPSHOT_MAX_BYTES."""
    import httpx

    monkeypatch.setattr(memory_repo, "_POOL_SNAPSHOT_MAX_BYTES", 16)

    class _FakeResp:
        status_code: ClassVar[int] = 200
        headers: ClassVar[dict[str, str]] = {}

        def iter_bytes(self) -> object:
            yield from [b"x" * 8, b"x" * 8, b"x" * 8]

    class _FakeStreamCtx:
        def __enter__(self) -> _FakeResp:
            return _FakeResp()

        def __exit__(self, *exc: object) -> bool:
            return False

    class _FakeClient:
        def __init__(self, **kwargs: object) -> None:
            pass

        def __enter__(self) -> _FakeClient:
            return self

        def __exit__(self, *exc: object) -> bool:
            return False

        def stream(self, *args: object, **kwargs: object) -> _FakeStreamCtx:
            return _FakeStreamCtx()

    monkeypatch.setattr(httpx, "Client", _FakeClient)

    with pytest.raises(memory_repo.MemoryPoolBootstrapFailed, match="exceeds"):
        memory_repo._download_pool_snapshot("http://gw/api/memory/pool", {})


def test_download_pool_snapshot_declared_oversize_rejected_before_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A Content-Length already over the cap is rejected before any body byte is
    read."""
    import httpx

    monkeypatch.setattr(memory_repo, "_POOL_SNAPSHOT_MAX_BYTES", 16)

    class _FakeResp:
        status_code: ClassVar[int] = 200
        headers: ClassVar[dict[str, str]] = {"Content-Length": "999999999"}

        def iter_bytes(self) -> object:
            raise AssertionError("body must not be read")

    class _FakeStreamCtx:
        def __enter__(self) -> _FakeResp:
            return _FakeResp()

        def __exit__(self, *exc: object) -> bool:
            return False

    class _FakeClient:
        def __init__(self, **kwargs: object) -> None:
            pass

        def __enter__(self) -> _FakeClient:
            return self

        def __exit__(self, *exc: object) -> bool:
            return False

        def stream(self, *args: object, **kwargs: object) -> _FakeStreamCtx:
            return _FakeStreamCtx()

    monkeypatch.setattr(httpx, "Client", _FakeClient)

    with pytest.raises(memory_repo.MemoryPoolBootstrapFailed, match="declared"):
        memory_repo._download_pool_snapshot("http://gw/api/memory/pool", {})


def test_download_pool_snapshot_transport_error_wrapped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """httpx transport errors are wrapped into the guided exception."""
    import httpx

    class _FailingClient:
        def __init__(self, **kwargs: object) -> None:
            pass

        def __enter__(self) -> _FailingClient:
            return self

        def __exit__(self, *exc: object) -> bool:
            return False

        def stream(self, *args: object, **kwargs: object) -> object:
            raise httpx.ConnectError("boom")

    monkeypatch.setattr(httpx, "Client", _FailingClient)

    with pytest.raises(memory_repo.MemoryPoolBootstrapFailed, match="failed: boom"):
        memory_repo._download_pool_snapshot("http://gw/api/memory/pool", {})
