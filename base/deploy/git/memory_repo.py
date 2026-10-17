from __future__ import annotations

import contextlib
import datetime
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from base.clock import Clock
from base.cluster.machine import MachineIdentity
from base.config import settings
from base.host.proc import run_bounded
from base.paths import gateway_memory_dir, memory_dir

_DEFAULT_GITIGNORE = """\
# Meiva memory pool — markdown notes only; swap / cache files do not enter history.
.DS_Store
*.swp
*.swo
*~
.cache/
"""


class MemoryRemoteMissing(RuntimeError):  # noqa: N818
    # meiva:link base/deploy/git/docs/memory-repo.meiva.okf.md
    pass


class MemoryPoolBootstrapFailed(RuntimeError):  # noqa: N818
    # meiva:link base/deploy/git/docs/memory-repo.meiva.okf.md
    pass


class MemoryRepoUninitialized(RuntimeError):  # noqa: N818
    # meiva:link base/deploy/git/docs/memory-repo.meiva.okf.md
    pass


class MemoryBranchMismatch(RuntimeError):  # noqa: N818
    # meiva:link base/deploy/git/docs/memory-repo.meiva.okf.md
    pass


def memory_remote() -> str:
    env = settings.general.memory_remote.strip()
    if env:
        return env
    raise MemoryRemoteMissing(
        "memory remote not set. Configure one before a remote-backed memory "
        "operation: set MEIVA_MEMORY_REMOTE=<git-url> in the home's `.env`."
    )


def branch_name(*, machine: MachineIdentity) -> str:
    # meiva:link base/deploy/git/docs/memory-repo.meiva.okf.md
    if "agent-runner" in machine.role():
        return f"machine-{machine.name()}"
    return "main"


def pull_main(*, root: Path, keep_local: bool, branch: str) -> str:
    # meiva:note
    # pull_main fast-forwards the supplied root from origin/<branch> and returns its HEAD. keep_local only reads HEAD, including when no remote or requested branch exists. Divergence fails without discarding local commits.
    # meiva:link base/deploy/git/docs/memory-repo.meiva.okf.md
    if keep_local:
        return run_git("rev-parse", "HEAD", cwd=root)
    run_git("fetch", "origin", branch, cwd=root)
    run_git("merge", "--ff-only", f"origin/{branch}", cwd=root)
    return run_git("rev-parse", "HEAD", cwd=root)


def is_initialized() -> bool:
    return (memory_dir() / ".git").is_dir()


def gateway_is_initialized(*, machine: MachineIdentity) -> bool:
    return (gateway_memory_dir(roles=machine.role()) / ".git").is_dir()


GIT_TIMEOUT_S = 120.0


def run_git(*args: str, cwd: Path | None = None) -> str:
    # meiva:note
    # run_git passes the per-command timeout to run_bounded, raises CalledProcessError for nonzero exits, and propagates TimeoutExpired. It returns stripped stdout only on success.
    # meiva:link base/deploy/git/docs/memory-repo.meiva.okf.md
    cwd_path = cwd if cwd is not None else memory_dir()
    argv = ["git", *args]

    result = run_bounded(
        argv,
        timeout=GIT_TIMEOUT_S,
        cwd=str(cwd_path),
        capture_output=True,
        text=True,
    )

    if result.returncode != 0:
        raise subprocess.CalledProcessError(
            result.returncode, argv, output=result.stdout, stderr=result.stderr
        )
    return result.stdout.strip()


def init_local_repo(branch: str, cwd: Path) -> None:
    # meiva:link base/deploy/git/docs/memory-repo.meiva.okf.md
    cwd.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(  # noqa: S603
        ["git", "init", "-q", "-b", branch, str(cwd)], check=True
    )
    run_git("config", "user.email", "meiva@local", cwd=cwd)
    run_git("config", "user.name", "Meiva Local", cwd=cwd)
    gitignore = cwd / ".gitignore"
    gitignore.write_text(_DEFAULT_GITIGNORE)
    run_git("add", ".gitignore", cwd=cwd)
    run_git("commit", "-q", "-m", f"init: local memory on {branch}", cwd=cwd)


def _strip_remotes(cwd: Path) -> None:
    for name in run_git("remote", cwd=cwd).split():
        run_git("remote", "remove", name, cwd=cwd)


POOL_SNAPSHOT_MAX_BYTES = 256 * 1024 * 1024
_POOL_FETCH_TIMEOUT_S = 120.0


def _stream_pool(client: Any, url: str, headers: dict[str, str]) -> tuple[str | None, bytes]:
    # meiva:link base/deploy/git/docs/memory-repo.meiva.okf.md
    with client.stream("GET", url, headers=headers) as resp:
        if resp.status_code != 200:
            raise MemoryPoolBootstrapFailed(f"GET {url} -> HTTP {resp.status_code}")
        declared = resp.headers.get("Content-Length")
        if declared and int(declared) > POOL_SNAPSHOT_MAX_BYTES:
            raise MemoryPoolBootstrapFailed(
                f"pool snapshot declared at {declared} bytes, over the {POOL_SNAPSHOT_MAX_BYTES}-byte cap"
            )
        buf = bytearray()
        for chunk in resp.iter_bytes():
            buf += chunk
            if len(buf) > POOL_SNAPSHOT_MAX_BYTES:
                raise MemoryPoolBootstrapFailed(
                    f"pool snapshot exceeds the {POOL_SNAPSHOT_MAX_BYTES}-byte cap"
                )
        head = resp.headers.get("X-Pool-Head")
        return head, bytes(buf)


def download_pool_snapshot(url: str, headers: dict[str, str]) -> tuple[str | None, bytes]:
    # meiva:link base/deploy/git/docs/memory-repo.meiva.okf.md
    import httpx

    try:
        with httpx.Client(timeout=_POOL_FETCH_TIMEOUT_S) as client:
            return _stream_pool(client, url, headers)
    except httpx.HTTPError as e:
        raise MemoryPoolBootstrapFailed(f"GET {url} failed: {e}") from e


def bootstrap_from_gateway(*, machine: MachineIdentity) -> None:
    # meiva:link base/deploy/git/docs/memory-repo.meiva.okf.md
    import shutil
    import tempfile

    from base.cluster.machine import gateway_api_base, gateway_auth_headers
    from base.cluster.machine.ambient import gateway_access_from_settings

    access = gateway_access_from_settings()  # ambient: B2
    try:
        base = gateway_api_base(access.gateway_url)
    except Exception as e:
        raise MemoryPoolBootstrapFailed(f"gateway URL unavailable: {e}") from e
    url = f"{base}/api/memory/pool"
    head, bundle_bytes = download_pool_snapshot(url, gateway_auth_headers(access))
    target = memory_dir()
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp_dir = Path(tempfile.mkdtemp(prefix=".memory-bootstrap-", dir=str(target.parent)))
    try:
        checkout_dir = _unpack_bundle(bundle_bytes, head, tmp_dir, machine=machine)
        if target.exists():
            shutil.rmtree(target, ignore_errors=True)
        checkout_dir.replace(target)
    except MemoryPoolBootstrapFailed:
        raise
    except (subprocess.CalledProcessError, OSError) as e:
        raise MemoryPoolBootstrapFailed(f"pool bootstrap failed: {e}") from e
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def _unpack_bundle(
    bundle_bytes: bytes, head: str | None, tmp_dir: Path, *, machine: MachineIdentity
) -> Path:
    # meiva:link base/deploy/git/docs/memory-repo.meiva.okf.md
    bundle_path = tmp_dir / "pool.bundle"
    bundle_path.write_bytes(bundle_bytes)
    checkout_dir = tmp_dir / "pool"
    result = run_bounded(
        ["git", "clone", "-q", str(bundle_path), str(checkout_dir)],
        timeout=GIT_TIMEOUT_S,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise MemoryPoolBootstrapFailed(
            f"git clone from bundle failed: {(result.stderr or '').strip()[:200]}"
        )
    if head:
        actual = run_git("rev-parse", "HEAD", cwd=checkout_dir)
        if actual != head:
            raise MemoryPoolBootstrapFailed(
                f"bundle HEAD {actual} does not match the advertised pool HEAD {head}"
            )

    run_git("remote", "remove", "origin", cwd=checkout_dir)
    run_git("checkout", "-q", "-b", branch_name(machine=machine), cwd=checkout_dir)
    return checkout_dir


@dataclass(frozen=True)
class RepoStatus:
    # meiva:link base/deploy/git/docs/memory-repo.meiva.okf.md
    branch: str
    is_clean: bool
    local_commits_on_branch: int
    ahead_of_main: int
    behind_main: int
    last_fetch: str


def _reconcile_initialized(branch: str, *, keep_local: bool, machine: MachineIdentity) -> None:
    # meiva:link base/deploy/git/docs/memory-repo.meiva.okf.md
    current = run_git("rev-parse", "--abbrev-ref", "HEAD")
    if current != branch:
        if "gateway" in machine.role() and "agent-runner" not in machine.role():
            if not keep_local:
                run_git("fetch", "origin", "main")
            run_git("checkout", "main")
            if keep_local:
                _strip_remotes(memory_dir())
            return
        raise MemoryBranchMismatch(
            f"{memory_dir()} is already a git repo but on branch {current!r}, expected {branch!r}. "
            f"Manually switch: `git -C {memory_dir()} checkout {branch}` "
            "(confirm working tree has no unpushed in-flight work)."
        )
    if keep_local:
        _strip_remotes(memory_dir())


def _init_without_remote(branch: str, *, machine: MachineIdentity) -> None:
    # meiva:link base/deploy/git/docs/memory-repo.meiva.okf.md
    if "agent-runner" in machine.role() and "gateway" not in machine.role():
        try:
            bootstrap_from_gateway(machine=machine)
        except MemoryPoolBootstrapFailed as e:
            raise MemoryRemoteMissing(
                "memory remote not configured and the gateway pool snapshot "
                f"fetch failed ({e}). Set MEIVA_MEMORY_REMOTE for the GitHub sync "
                "path, fix the gateway URL / cluster secret, or set "
                "MEIVA_MEMORY_KEEP_LOCAL=true "
                "to run an explicitly local-only pool."
            ) from e
        return

    init_local_repo(branch, memory_dir())


def init(*, machine: MachineIdentity) -> None:
    # meiva:link base/deploy/git/docs/memory-repo.meiva.okf.md
    branch = branch_name(machine=machine)
    keep_local = settings.general.memory_keep_local
    if is_initialized():
        _reconcile_initialized(branch, keep_local=keep_local, machine=machine)
        return

    if keep_local:
        init_local_repo(branch, memory_dir())
        return

    memory_dir().parent.mkdir(parents=True, exist_ok=True)
    try:
        remote = memory_remote()
    except MemoryRemoteMissing:
        _init_without_remote(branch, machine=machine)
        return

    subprocess.run(  # noqa: S603
        ["git", "clone", remote, str(memory_dir())],
        check=True,
    )

    remote_branches = run_git("ls-remote", "--heads", "origin", branch)
    if remote_branches:
        run_git("checkout", branch)
    else:
        run_git("checkout", "-b", branch)
        run_git("push", "-u", "origin", branch)

    gitignore = memory_dir() / ".gitignore"
    if not gitignore.exists():
        gitignore.write_text(_DEFAULT_GITIGNORE)
        run_git("add", ".gitignore")
        run_git("commit", "-m", f"init: .gitignore on {branch}")
        run_git("push")


def init_gateway(*, machine: MachineIdentity) -> None:
    # meiva:link base/deploy/git/docs/memory-repo.meiva.okf.md
    branch = "main"
    gmd = gateway_memory_dir(roles=machine.role())
    keep_local = settings.general.memory_keep_local
    if gateway_is_initialized(machine=machine):
        current = run_git("rev-parse", "--abbrev-ref", "HEAD", cwd=gmd)
        if current != branch:
            if not keep_local:
                run_git("fetch", "origin", "main", cwd=gmd)
            run_git("checkout", "main", cwd=gmd)
        if keep_local:
            _strip_remotes(gmd)
        return

    if keep_local:
        init_local_repo(branch, gmd)
        return

    gmd.parent.mkdir(parents=True, exist_ok=True)
    try:
        remote = memory_remote()
    except MemoryRemoteMissing:
        init_local_repo(branch, gmd)
        return

    subprocess.run(  # noqa: S603
        ["git", "clone", remote, str(gmd)],
        check=True,
    )

    remote_branches = run_git("ls-remote", "--heads", "origin", branch, cwd=gmd)
    if remote_branches:
        run_git("checkout", branch, cwd=gmd)
    else:
        run_git("checkout", "-b", branch, cwd=gmd)
        run_git("push", "-u", "origin", branch, cwd=gmd)

    gitignore = gmd / ".gitignore"
    if not gitignore.exists():
        gitignore.write_text(_DEFAULT_GITIGNORE)
        run_git("add", ".gitignore", cwd=gmd)
        run_git("commit", "-m", f"init: .gitignore on {branch}", cwd=gmd)
        run_git("push", cwd=gmd)


def status() -> RepoStatus:
    # meiva:link base/deploy/git/docs/memory-repo.meiva.okf.md
    if not is_initialized():
        raise MemoryRepoUninitialized(
            f"{memory_dir()} is not yet a git repo — run `meiva memory init` first."
        )

    branch = run_git("rev-parse", "--abbrev-ref", "HEAD")
    is_clean = run_git("status", "--porcelain") == ""
    local_commits = int(run_git("rev-list", "--count", "HEAD"))

    ahead = behind = 0
    with contextlib.suppress(subprocess.CalledProcessError):
        from base.config import settings

        counts = run_git(
            "rev-list", "--left-right", "--count", f"HEAD...origin/{settings.general.track_branch}"
        )
        ahead_s, behind_s = counts.split()
        ahead, behind = int(ahead_s), int(behind_s)

    fetch_head = memory_dir() / ".git" / "FETCH_HEAD"
    if fetch_head.exists():
        last_fetch = (
            datetime.datetime.fromtimestamp(
                fetch_head.stat().st_mtime,
                tz=datetime.UTC,
            )
            .astimezone(Clock.from_settings().zone())
            .isoformat(timespec="seconds")
        )
    else:
        last_fetch = "never"

    return RepoStatus(
        branch=branch,
        is_clean=is_clean,
        local_commits_on_branch=local_commits,
        ahead_of_main=ahead,
        behind_main=behind,
        last_fetch=last_fetch,
    )
