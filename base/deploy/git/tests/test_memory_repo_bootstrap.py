"""`base/deploy/git/memory_repo.py` status + pool-bootstrap tests: the cluster-zone
display of last_fetch, and the no-remote split-runner bootstrap from a gateway bundle
with its failure paths; split from base/deploy/git/tests/test_memory_repo.py (task #4922)."""

from __future__ import annotations

import subprocess
from collections.abc import Generator
from pathlib import Path

import pytest

from base.cluster.machine import reset_identity, set_identity
from base.config import settings
from base.deploy.git import memory_repo


@pytest.fixture(autouse=True)
def _identity_reset() -> Generator[None, None, None]:
    """Reset injected machine identity before and after each test so role/name
    injection never leaks across tests."""
    reset_identity()
    yield
    reset_identity()


def test_status_last_fetch_renders_cluster_zone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """RepoStatus.last_fetch is a display timestamp: it renders in the cluster
    timezone (user ruling 2026-08-27), never the host OS zone."""

    import datetime as dt

    general = type(settings.general).model_construct(timezone="Asia/Shanghai")
    monkeypatch.setattr(settings, "general", general)
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
    from base.paths import memory_dir as _md

    head, bundle_bytes = _make_real_bundle(tmp_path / "src")

    captured: dict[str, object] = {}

    def _fake_download(url: str, headers: dict[str, str]) -> tuple[str | None, bytes]:
        captured["url"] = url
        captured["headers"] = headers
        return head, bundle_bytes

    monkeypatch.setattr(memory_repo, "_download_pool_snapshot", _fake_download)

    import base.cluster.machine as _machine

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

    import base.cluster.machine as _machine

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

    import base.cluster.machine as _machine

    monkeypatch.setattr(_machine, "gateway_api_base", lambda: "http://gw")
    set_identity(role="agent-runner", name="test-host-2")
    with pytest.raises(memory_repo.MemoryPoolBootstrapFailed, match="HTTP 502"):
        memory_repo.bootstrap_from_gateway()


def test_bootstrap_from_gateway_missing_gateway_url_fails_loud(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A runner with no gateway URL configured fails with guidance, not the
    raw GatewayApiBaseMissing traceback."""
    import base.cluster.machine as _machine

    monkeypatch.setattr(
        _machine,
        "gateway_api_base",
        lambda: (_ for _ in ()).throw(
            _machine.GatewayApiBaseMissing("gateway_url unset — `ava start` writes it")
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

    import base.cluster.machine as _machine

    monkeypatch.setattr(_machine, "gateway_api_base", lambda: "http://gw.example:8000")
    set_identity(role="agent-runner", name="test-host-2")
    with pytest.raises(memory_repo.MemoryPoolBootstrapFailed, match="git clone from bundle failed"):
        memory_repo.bootstrap_from_gateway()
