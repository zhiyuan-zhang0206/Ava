"""The throwaway-Postgres orphan sweep + serialized port allocation
(`shared/pg_tools.py`).

Two directions, and the refusal is the important one:

- **Reaps** an instance whose owner was SIGKILLed. The one end-to-end test starts a
  real throwaway cluster in a child process, kills the child, shows the postmaster
  outlives it (that IS the leak — a killed process runs no finalizer), then sweeps.
- **Refuses** anything still live, and refuses to select a real cluster's data dir
  at all. A real cluster's `$AVA_HOME/pg` never carries an owner lock, and
  `_resolved_throwaway_dir` re-verifies the lock's parent, so the refusals are
  asserted on real-cluster-*shaped* scratch dirs and on the pure predicate — never
  by pointing a sweep at the operator's `~/.ava` and hoping.

The lock lives inside the instance dir it protects, which is what makes two failure
modes untestable-because-unreachable rather than defended: it cannot be pruned
while its cluster survives (same directory, same age), and it cannot collide with
another UNIX user on a shared tmpfs (`mkdtemp` makes the instance dir `0700`, so the
glob never sees theirs — asserted below).

Every test pins the throwaway root at a scratch dir, so a sweep here can only see
locks the test itself made — never this worker's own live session cluster.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterator
from pathlib import Path

import psycopg
import pytest

from shared import pg_tools
from shared.paths import repo_root
from shared.platform import IS_WINDOWS

pytestmark = pytest.mark.skipif(
    IS_WINDOWS, reason="the registry lock is POSIX flock; the sweep is a no-op on Windows"
)

_READY_TIMEOUT_S = 120.0


def _write_instance(root: Path, name: str) -> Path:
    """A throwaway-shaped instance dir (`<root>/<name>/data` + PG_VERSION) with no
    server behind it — enough for the sweep to treat it as reapable."""
    data = root / name / "data"
    data.mkdir(parents=True)
    (data / "PG_VERSION").write_text("17\n")
    return root / name


def _write_lock(instance_dir: Path) -> Path:
    """An unlocked owner lock — i.e. exactly what a killed owner leaves behind."""
    lock = instance_dir / pg_tools._OWNER_LOCK_NAME
    lock.write_bytes(b'{"owner_pid": 0, "port": 0}')
    return lock


def _wait_for(path: Path) -> None:
    deadline = time.monotonic() + _READY_TIMEOUT_S
    while not path.exists():
        if time.monotonic() >= deadline:
            raise AssertionError(f"{path} never appeared within {_READY_TIMEOUT_S}s")
        time.sleep(0.05)


@pytest.fixture
def throwaway_root(monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Point the throwaway root (instance dirs + registry) at a scratch dir.

    Deliberately NOT `tmp_path`: the Postgres socket path (`<dir>/.s.PGSQL.<port>`)
    is capped at 103 bytes and pytest's per-test dir is far too deep for a real
    cluster to start beneath it — the same cap `_cluster_instance._pg_socket_dir`
    works around. A short `/tmp/ava-sweep-*` root leaves room. Scratch files that
    are not instance dirs still go in `tmp_path`.

    Teardown force-stops anything still running under the root, so a failing test
    cannot leak the very orphan this module exists to prevent."""
    root = Path(tempfile.mkdtemp(prefix="ava-sweep-", dir="/tmp"))
    monkeypatch.setattr(pg_tools, "_tmpfs_base", str(root))
    yield root
    for data in root.glob("ava-pg-*/data"):
        if (data / "PG_VERSION").is_file():
            subprocess.run(  # noqa: S603 — argv is the resolved pg_ctl path + this test's own dir
                [pg_tools.pg_tool("pg_ctl"), "-D", str(data), "-m", "immediate", "stop"],
                check=False,
                capture_output=True,
            )
    shutil.rmtree(root, ignore_errors=True)


# ── Direction 1: a genuinely orphaned instance is reaped ──

_ORPHAN_OWNER = """
import os, sys, time
import shared.pg_tools as pg_tools

pg_tools._tmpfs_base = sys.argv[1]
# `cm` must stay referenced: a dropped context manager is finalized, and its
# GeneratorExit would run the very teardown this test needs never to happen.
cm = pg_tools.throwaway_postgres()
url = cm.__enter__()
open(sys.argv[2] + ".part", "w").write(url)
os.replace(sys.argv[2] + ".part", sys.argv[2])
time.sleep(600)
"""


def test_sweep_reaps_an_instance_whose_owner_was_killed(
    throwaway_root: Path, tmp_path: Path
) -> None:
    url_file = tmp_path / "url"
    owner = subprocess.Popen(  # noqa: S603 — this interpreter + a literal script
        [sys.executable, "-c", _ORPHAN_OWNER, str(throwaway_root), str(url_file)],
        cwd=repo_root(),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        _wait_for(url_file)
        url = url_file.read_text()
        with psycopg.connect(url) as conn:
            conn.execute("select 1")
        [instance] = list(throwaway_root.glob("ava-pg-*"))
        lock = instance / pg_tools._OWNER_LOCK_NAME
        assert lock.is_file()  # the lock sits beside the data dir it protects

        owner.kill()
        owner.wait(timeout=10)
        # The leak itself: the postmaster is detached, so it outlives the SIGKILLed
        # owner, which ran no finalizer, no atexit handler and no signal handler.
        with psycopg.connect(url) as conn:
            conn.execute("select 1")

        assert pg_tools.sweep_orphaned_throwaway_clusters() == 1
        assert not instance.exists()
        assert not lock.exists()
        with pytest.raises(psycopg.OperationalError):
            psycopg.connect(url, connect_timeout=5)
    finally:
        owner.kill()


def test_sweep_reaps_an_instance_whose_lock_was_released(throwaway_root: Path) -> None:
    """The same reap without the initdb: releasing the lock is exactly what the
    kernel does for a killed owner, so the leftover instance is reaped."""
    instance = _write_instance(throwaway_root, "ava-pg-dead")
    registration = pg_tools._register_throwaway(instance, 5555)
    assert registration is not None
    os.close(registration.fd)  # the owner "dies": lock released, files still on disk

    assert pg_tools.sweep_orphaned_throwaway_clusters() == 1
    assert not instance.exists()
    assert not registration.lock.exists()


# ── Direction 2: nothing live, and no real cluster, is ever selected ──

_LIVE_OWNER = """
import fcntl, os, sys, time

fd = os.open(sys.argv[1], os.O_RDWR | os.O_CREAT, 0o600)
fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
os.write(fd, b'{"owner_pid": 0, "port": 0}')
open(sys.argv[2], "w").close()
time.sleep(600)
"""


def test_sweep_refuses_an_instance_owned_by_a_live_process(
    throwaway_root: Path, tmp_path: Path
) -> None:
    """The flock is the liveness oracle: another process still holding it means the
    instance is in use, whatever any recorded pid says."""
    instance = _write_instance(throwaway_root, "ava-pg-live")
    lock = instance / pg_tools._OWNER_LOCK_NAME
    ready = tmp_path / "ready"
    owner = subprocess.Popen(  # noqa: S603 — this interpreter + a literal script
        [sys.executable, "-c", _LIVE_OWNER, str(lock), str(ready)]
    )
    try:
        _wait_for(ready)
        assert pg_tools.sweep_orphaned_throwaway_clusters() == 0
        assert (instance / "data" / "PG_VERSION").is_file()
        assert lock.exists()
    finally:
        owner.kill()
        owner.wait(timeout=10)


def test_sweep_refuses_an_instance_this_process_owns(throwaway_root: Path) -> None:
    """A worker's own live cluster survives its own sweep: flock conflicts between
    two open file descriptions even inside one process."""
    instance = _write_instance(throwaway_root, "ava-pg-mine")
    registration = pg_tools._register_throwaway(instance, 5556)
    try:
        assert pg_tools.sweep_orphaned_throwaway_clusters() == 0
        assert (instance / "data" / "PG_VERSION").is_file()
    finally:
        pg_tools._unregister_throwaway(registration)


def test_registration_lock_is_not_inherited_by_subprocesses(throwaway_root: Path) -> None:
    """Close-on-exec, so a leaked test subprocess cannot hold the lock open and pin
    a finished instance as forever-unreapable."""
    registration = pg_tools._register_throwaway(_write_instance(throwaway_root, "ava-pg-x"), 1)
    assert registration is not None
    try:
        assert os.get_inheritable(registration.fd) is False
    finally:
        pg_tools._unregister_throwaway(registration)


def test_a_real_cluster_data_dir_is_never_a_throwaway_dir(
    throwaway_root: Path, tmp_path: Path
) -> None:
    """The pure predicate every reap goes through, on real-cluster shapes: an
    `$AVA_HOME` (the operator's own, and a scratch look-alike) is refused because it
    is neither `ava-pg-`-named nor a child of the throwaway root, and a plain
    non-prefixed dir inside the root is refused too."""
    fake_home = tmp_path / ".ava"
    (fake_home / "pg").mkdir(parents=True)
    (fake_home / "pg" / "PG_VERSION").write_text("17\n")

    assert pg_tools._resolved_throwaway_dir(Path.home() / ".ava") is None
    assert pg_tools._resolved_throwaway_dir(Path.home() / ".ava" / "pg") is None
    assert pg_tools._resolved_throwaway_dir(fake_home) is None
    assert pg_tools._resolved_throwaway_dir(fake_home / "pg") is None
    (throwaway_root / "pg").mkdir()
    assert pg_tools._resolved_throwaway_dir(throwaway_root / "pg") is None
    # A worktree cluster home can even *contain* the prefix (`~/.ava-pg-<worktree>`,
    # which is exactly what `install.sh --worktree` names this branch's own home).
    # Still refused: the name must START with the prefix, and no cluster home is a
    # child of the throwaway root.
    worktree_home = tmp_path / ".ava-pg-worktree"
    (worktree_home / "pg").mkdir(parents=True)
    assert pg_tools._resolved_throwaway_dir(worktree_home) is None
    assert pg_tools._resolved_throwaway_dir(worktree_home / "pg") is None
    # The positive case, so the refusals above are not vacuous.
    instance = _write_instance(throwaway_root, "ava-pg-real")
    assert pg_tools._resolved_throwaway_dir(instance) == instance.resolve()


def test_sweep_refuses_a_symlink_aimed_out_of_the_throwaway_root(
    throwaway_root: Path, tmp_path: Path
) -> None:
    """A throwaway-*named* dir that is really a symlink to a cluster home, and a
    correctly-named dir whose `data` is a symlink to a cluster's pg dir. Both are
    refused, so neither `pg_ctl stop` nor the rmtree can be aimed outside the root.
    The stray locks are dropped; every target survives untouched — including the
    fake cluster home the first symlink points at, which is where writing through
    that symlink actually put its lock."""
    fake_home = tmp_path / ".ava"
    real_pg = fake_home / "pg"
    real_pg.mkdir(parents=True)
    (real_pg / "PG_VERSION").write_text("17\n")

    linked = throwaway_root / "ava-pg-linked"
    linked.symlink_to(fake_home, target_is_directory=True)
    trap = throwaway_root / "ava-pg-trap"
    trap.mkdir()
    (trap / "data").symlink_to(real_pg, target_is_directory=True)
    _write_lock(trap)
    _write_lock(linked)  # lands inside fake_home, through the symlink

    assert pg_tools.sweep_orphaned_throwaway_clusters() == 0
    assert (real_pg / "PG_VERSION").is_file()  # the cluster's data dir, untouched
    assert fake_home.is_dir()  # and its home
    assert trap.is_dir()  # the trap dir is left alone, not reaped
    # Only the stray lock files themselves are dropped.
    assert not (fake_home / pg_tools._OWNER_LOCK_NAME).exists()
    assert not (trap / pg_tools._OWNER_LOCK_NAME).exists()


def test_a_lock_body_cannot_aim_the_sweep(throwaway_root: Path, tmp_path: Path) -> None:
    """The reap target is the lock file's own parent, so a body naming a real
    cluster's data dir is inert — nothing is read out of it but the log detail. And a
    lock in a non-prefixed dir is not even a sweep candidate."""
    real_pg = tmp_path / ".ava" / "pg"
    real_pg.mkdir(parents=True)
    (real_pg / "PG_VERSION").write_text("17\n")
    liar = _write_instance(throwaway_root, "ava-pg-liar")
    (liar / pg_tools._OWNER_LOCK_NAME).write_text(
        f'{{"owner_pid": 0, "port": 0, "data_dir": "{real_pg}"}}'
    )
    bystander = throwaway_root / "not-a-throwaway"
    bystander.mkdir()
    _write_lock(bystander)

    assert pg_tools.sweep_orphaned_throwaway_clusters() == 1  # the liar, by its parent
    assert (real_pg / "PG_VERSION").is_file()  # the path in its body, untouched
    assert not liar.exists()
    assert (bystander / pg_tools._OWNER_LOCK_NAME).exists()  # never a candidate


def test_sweep_is_a_noop_on_an_empty_root(throwaway_root: Path) -> None:
    assert pg_tools.sweep_orphaned_throwaway_clusters() == 0


def test_sweep_ignores_an_instance_dir_with_no_owner_lock(throwaway_root: Path) -> None:
    """An instance leaked before this mechanism existed carries no lock, so it is not
    a candidate. Claiming it would mean guessing by exclusion, which is exactly what
    the positive identification exists to avoid."""
    instance = _write_instance(throwaway_root, "ava-pg-legacy")
    assert pg_tools.sweep_orphaned_throwaway_clusters() == 0
    assert (instance / "data" / "PG_VERSION").is_file()


def test_another_users_instance_dir_is_invisible_to_the_sweep(throwaway_root: Path) -> None:
    """On a shared tmpfs (`/dev/shm`) instance dirs are `0700`, so one user's sweep
    cannot even enumerate another's — no contention, and nothing to refuse. A mode
    `0o000` dir stands in for a foreign UID's, which this test cannot create.

    This is why the lock lives in the instance dir: one shared registry directory,
    created `0700` by whoever ran tests first, would instead have made the second
    user's every run fail outright on an unwritable directory."""
    mine = _write_instance(throwaway_root, "ava-pg-mine")
    _write_lock(mine)
    theirs = _write_instance(throwaway_root, "ava-pg-theirs")
    _write_lock(theirs)
    theirs.chmod(0o000)
    try:
        assert pg_tools.sweep_orphaned_throwaway_clusters() == 1  # mine only
        assert not mine.exists()
    finally:
        theirs.chmod(0o700)
    assert (theirs / "data" / "PG_VERSION").is_file()


def test_two_sweepers_reap_an_instance_once(throwaway_root: Path) -> None:
    """The deadness proof is also the reap lock: a second sweeper racing the first
    finds the lock held, so no instance is reaped twice."""
    import fcntl

    instance = _write_instance(throwaway_root, "ava-pg-raced")
    record = _write_lock(instance)
    held = os.open(record, os.O_RDWR)
    try:
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)  # stand in for the first sweeper
        assert pg_tools.sweep_orphaned_throwaway_clusters() == 0
        assert instance.exists()
    finally:
        os.close(held)
    assert pg_tools.sweep_orphaned_throwaway_clusters() == 1


# ── Direction 3: port allocation is serialized across workers ──
#
# `_allocate_port` closes the `_free_port` TOCTOU: probe + registry check +
# registration all happen under one host-wide flock, so a port a live cluster has
# been handed but not yet bound (its postmaster is still in initdb) never goes to
# a second worker. `throwaway_postgres` additionally retries a start that loses
# the race anyway (a non-registry holder bound the port) on a fresh port.


def test_allocate_port_skips_a_live_clusters_port(
    throwaway_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The registry is what closes the window: a port held by a live cluster's
    registration must not be handed out, even though the OS would still give it
    (nothing has bound it yet). The probe is pointed first at the held port, then
    at a genuinely free one; the allocator must refuse the former and take the
    latter."""
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        held = s.getsockname()[1]
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        free = s.getsockname()[1]

    live = _write_instance(throwaway_root, "ava-pg-live-port")
    registration = pg_tools._register_throwaway(live, held)
    probes = iter([held, free])
    monkeypatch.setattr(pg_tools, "_free_port", lambda: next(probes))
    try:
        mine = _write_instance(throwaway_root, "ava-pg-alloc-port")
        port, mine_reg = pg_tools._allocate_port(mine)
        try:
            assert port == free
        finally:
            pg_tools._unregister_throwaway(mine_reg)
        shutil.rmtree(mine, ignore_errors=True)
    finally:
        pg_tools._unregister_throwaway(registration)
        shutil.rmtree(live, ignore_errors=True)


def test_start_retries_with_a_fresh_port_after_a_collision(
    throwaway_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The pragmatic half of the fix: when `pg_ctl start` loses the race anyway
    (here: a non-registry holder — the test's own listening socket — took the
    port), the start is retried on a fresh port instead of failing the fixture.
    The first probe hands out the blocked port, the retry's probe a real free
    one, and the cluster must come up and serve."""
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as blocker:
        blocker.bind(("127.0.0.1", 0))
        blocker.listen(1)
        blocked = blocker.getsockname()[1]
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("127.0.0.1", 0))
            free = s.getsockname()[1]
        probes = iter([blocked, free])
        monkeypatch.setattr(pg_tools, "_free_port", lambda: next(probes))
        with pg_tools.throwaway_postgres() as url, psycopg.connect(url) as conn:
            conn.execute("select 1")
        # The blocker survived the whole retry dance untouched.
        assert blocker.getsockname()[1] == blocked
