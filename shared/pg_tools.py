"""Locate native Postgres binaries across the two supported host platforms,
and spin up throwaway clusters for tests.

macOS runs Homebrew's keg-only `postgresql@17` (binaries are not symlinked
onto PATH, so the full keg path is required); Linux runs the apt
`postgresql-17` layout; Windows uses the EDB installer path. Shared by the
per-cluster data-plane bring-up (`cli/commands/_cluster_instance.py`), the local
backup path (`services/backup.py`), and the throwaway clusters the test suite
(`tests/_containers.py`), migration smoke (`scripts/migration_smoke.py`), and eval
fixtures spin up.

Throwaway clusters are also **self-limiting when their owner is killed** — see
the registry/sweep block below `throwaway_postgres`.
"""

from __future__ import annotations

import contextlib
import json
import os
import shlex
import shutil
import socket
import subprocess
import sys
import tempfile
from collections.abc import Generator
from contextlib import closing, contextmanager
from functools import cache
from pathlib import Path
from typing import NamedTuple, TypedDict, cast

from shared import pg_throwaway_base as _throwaway_base
from shared.log import logger
from shared.pg_foreground import (
    start_foreground_postgres,
    stop_foreground_postgres,
    wait_foreground_postgres,
)
from shared.pg_stall_watchdog import fixture_log_artifact_dir, stall_guard
from shared.platform import IS_MACOS, IS_WINDOWS
from shared.process_env import inherited_process_env

PG_BIN_LINUX = Path("/usr/lib/postgresql/17/bin")
PG_BIN_WINDOWS = Path("C:\\Program Files\\PostgreSQL\\17\\bin")  # EDB installer default

# Historical private name: tests monkeypatch it to redirect artifact writes.
_fixture_log_artifact_dir = fixture_log_artifact_dir


def is_macos() -> bool:
    """True on macOS. Thin re-export of shared.platform.IS_MACOS, kept because
    cli/commands/_cluster_instance.py imports this name."""
    return IS_MACOS


# mmap-backed shared memory for every Postgres this codebase starts (Task #1263).
# Both settings make Postgres keep shared memory in files under the data
# directory instead of POSIX shm segments in /dev/shm, so an external unlink of
# /dev/shm cannot take a running instance down (the staging incident that
# motivated the task; the machine-side fix was the same two settings).
_PG_SHM_ARGS = "-c shared_memory_type=mmap -c dynamic_shared_memory_type=mmap"

# macOS compatibility finding (Task #1263): verified 2026-08-13 on the vendored
# PG 17.4 — both options are accepted and effective (pg_settings, source=command
# line); mmap is already the default for the main region on macOS and DSM mmap
# has been supported since PG 15. If a future macOS PG build rejects either
# option, flip this to False: `pg_shm_args` then keeps macOS on its status quo
# (no explicit settings) while Linux/WSL keeps the pin — the fallback the task
# named ("incompatible -> Linux/WSL only").
_PG_SHM_MMAP_OK_ON_MACOS = True


def pg_shm_args() -> str:
    """The `pg_ctl -o` fragments that pin mmap-backed shared memory.

    Returns the two `-c` settings (no surrounding spaces) that every PG startup
    path passes on the `pg_ctl start` command line: the per-cluster data plane
    (`cli/commands/_cluster_instance.py`) and the throwaway test/eval clusters
    (`throwaway_postgres`). Command-line `-c` outranks anything a machine's
    postgresql.conf says, so Ava's instances carry the posture no matter what
    the host is configured with. A platform that must not set them yields "" —
    callers splice the return value into their `-o` string, so empty is a no-op.
    """
    if is_macos() and not _PG_SHM_MMAP_OK_ON_MACOS:
        return ""
    return _PG_SHM_ARGS


# Postgres session timezone, pinned to UTC. psycopg3 returns aware datetimes in
# the SESSION timezone (never pinned before this), so without this every
# timestamptz the gateway/tests see round-trips with an offset that drifts
# with the host OS timezone instead of being a stable `+00:00`. Same splice
# points as `pg_shm_args` above: the per-cluster data plane and the throwaway
# test/eval clusters.
_PG_TZ_ARGS = "-c timezone=UTC"


def pg_tz_args() -> str:
    """The `pg_ctl -o` fragment that pins the Postgres session timezone to UTC.

    See `_PG_TZ_ARGS` above for why. Unlike `pg_shm_args`, this one has no
    platform exception — every PG startup path in this codebase sets it."""
    return _PG_TZ_ARGS


# macOS postmaster locale fallback (Task #3754). With no LC_ALL/LANG in the
# environment, locale init goes through CoreFoundation, which spawns a thread,
# and the postmaster refuses to run multithreaded: it aborts with "postmaster
# became multithreaded during startup" (HINT: set LC_ALL to a valid locale).
# A launchd job and a non-interactive ssh session both start `ava` with no
# locale at all — so PG is handed one instead of inheriting the caller's
# emptiness. Verified 2026-09-17 on macOS against Homebrew postgresql@17
# (17.11): nothing at all fails, an empty LC_ALL fails, LC_CTYPE alone fails
# (a locale-less Python start fills exactly that one variable via its own
# C-locale coercion — not enough), any non-empty LC_ALL/LANG starts; initdb
# needs none of this. The environment
# stays the override (a caller that sets LC_ALL/LANG keeps it verbatim), and
# Linux gets nothing — its absent locale resolves to C without threads, and a
# minimal image may not even have en_US.UTF-8 generated.
_MACOS_PG_LOCALE = "en_US.UTF-8"


def pg_start_env() -> dict[str, str]:
    """The child environment for starting a Postgres server.

    The caller's live environment, with `LC_ALL` supplied when it carries
    neither `LC_ALL` nor `LANG` (macOS only — see `_MACOS_PG_LOCALE`). Pass
    this as `env=` to the `pg_ctl start` invocation (or a direct `postgres`
    spawn): pg_ctl hands its environment to the postmaster, and the macOS
    postmaster aborts at startup when that environment has no locale."""
    env = inherited_process_env()
    if is_macos() and not env.get("LC_ALL") and not env.get("LANG"):
        env["LC_ALL"] = _MACOS_PG_LOCALE
    return env


# Prefix of every throwaway instance directory this module creates. Load-bearing:
# the sweep below reaps ONLY `<a throwaway root>/<this prefix>*` directories.
_THROWAWAY_PREFIX = "ava-pg-"

# Where throwaway instance dirs live — the platform default (/dev/shm on Linux,
# else the OS temp dir), the AVA_PG_THROWAWAY_BASE override, and the
# capacity-aware pick — is shared.pg_throwaway_base, extracted to keep this
# module under its line ceiling.


@cache
def brew_prefix(formula: str = "") -> Path:
    """`brew --prefix [formula]`. Bare form → the Homebrew root (`/opt/homebrew`
    on Apple Silicon, `/usr/local` on Intel); with a formula → that keg's path.
    Falls back to the Apple-Silicon default if brew is absent so callers degrade
    to a wrong-but-harmless path (probes then read not-running) instead of crashing."""
    cmd = ["brew", "--prefix", *([formula] if formula else [])]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, check=False)  # noqa: S603
    except FileNotFoundError:
        return Path("/opt/homebrew")
    return Path(out.stdout.strip() or "/opt/homebrew")


def pg_tool(name: str) -> Path:
    """Path to a Postgres client/server binary.

    Resolution: the vendored relocatable Postgres under `~/.ava/runtime/` is
    preferred when it actually carries `name` (`shared.runtime_binaries`, fetched
    by converge), so a clean machine needs no `brew install postgresql@17`. The
    vendored tree is a minimal server distribution — only `initdb` / `pg_ctl` /
    `postgres`; client tools (`pg_isready` / `psql` / `pg_dump`) are absent, so
    those always fall through to the host install below. Same fall-through when the
    vendored tree is missing entirely (a dev box not yet converged):
    - macOS: Homebrew keg-only postgresql@17 path.
    - Linux: apt postgresql-17 layout under /usr/lib/postgresql/17/bin.
    - Windows: EDB installer default path.
    - Other: PATH lookup via shutil.which (best-effort fallback)."""
    from shared.runtime_binaries import vendored_pg_bin_dir

    vendored = vendored_pg_bin_dir()
    if vendored is not None and (vendored / name).exists():
        return vendored / name
    from shared.platform_backend import get_backend

    platform_path = get_backend().pg_binary_path(name)
    if platform_path is not None and platform_path.exists():
        return platform_path
    # Fall back to PATH lookup (user may have added PG bin to PATH)
    found = shutil.which(name)
    if found:
        return Path(found)
    raise RuntimeError(
        f"Cannot locate pg tool {name!r} on {sys.platform} — "
        "install PostgreSQL 17 or set AVA_PG_* (AVA_PG_BIN etc.) for a custom pg install"
    )


def _free_port() -> int:
    """Ask the OS for an unused localhost TCP port. The socket is closed
    immediately, so the port is only a PROBE — it is not reserved; `_allocate_port`
    makes the reservation atomic."""
    with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# ── Orphaned throwaway clusters: owner lock + start-of-run sweep ──
#
# A throwaway postmaster is DETACHED — `pg_ctl` exits as soon as it is up, so the
# postmaster is already reparented away from the run that asked for it. Killing the
# owner (Ctrl-C, SIGKILL, an agent dying mid-run) therefore leaves it running with
# nothing left to stop it, and **no teardown path can fix that**: a SIGKILLed
# process runs no `finally`, no `atexit` handler and no signal handler. Each
# survivor holds one System V shared-memory segment (Postgres' startup interlock)
# and macOS ships `kern.sysv.shmmni=32`, so after ~31 interrupted runs the box
# wedges and NO cluster — test or real — can start at all.
#
# The durable mechanism is therefore a sweep at the start of the NEXT spin-up:
# every throwaway instance drops an owner lock INSIDE its own instance dir, and the
# next `throwaway_postgres` reaps the instances whose owner is provably gone.
#
# Liveness oracle: an exclusive `flock` the owner holds on that lock file for the
# instance's whole life. The kernel releases it when the owner dies, however it
# dies. A sweeper that takes `LOCK_EX | LOCK_NB` has therefore PROVEN that no live
# process owns that instance — it never compares pids, so a pid recycled onto an
# unrelated process cannot make a live instance look dead. The lock fd is
# close-on-exec (Python's default), so a test subprocess does not inherit the lock
# and pin a finished instance. Being both the deadness proof and the reap lock, it
# also makes concurrent sweepers (every xdist worker calls this) safe for free.
#
# The lock lives in the instance dir rather than in a side registry, which is what
# makes two otherwise-real failure modes unreachable:
#
# - **It cannot outlive, or be outlived by, the cluster it describes.** A side
#   registry can be pruned by a tmp cleaner while the instance it names keeps
#   running, and that instance is then permanently unsweepable — this leak class,
#   reopened by another route. Here the lock and the data dir are the same
#   directory with the same age, so a cleaner that can delete the lock is already
#   deleting files under a live postmaster (a far louder failure, and one that
#   predates any of this).
# - **No cross-user contention.** `mkdtemp` creates the instance dir `0700`, so on
#   a shared tmpfs (`/dev/shm` on Linux, `/var/tmp` on the disk fallback) another
#   user's instances are invisible to
#   this user's glob and vice versa — correctly, since neither could stop the
#   other's postmaster anyway. A single well-known registry dir owned by whoever
#   ran tests first would instead have made the SECOND user's every run fail on an
#   unwritable directory.
#
# Safety — a real cluster can never be selected. The reap target is not read from
# the lock's *contents*; it is the lock file's own parent directory, re-verified
# against the shape this module itself creates (`_resolved_throwaway_dir`): the name
# carries `_THROWAWAY_PREFIX`, the resolved directory is a direct child of the
# throwaway root under its own name, and `data` is not a symlink. For a real
# cluster to be selected its `$AVA_HOME` would have to BE an `ava-pg-*` child of
# the throwaway root and keep its data dir at `data` instead of `pg` — two
# independent misses — and no symlink can redirect a verified path back out. The
# only privileged act is `pg_ctl -D <verified data dir> stop`, which takes the pid
# it signals from that data dir's own `postmaster.pid`: no pid this module recorded
# is ever signalled.

# Owner lock, one per instance dir, beside that instance's `data/` and `pg.log`.
_OWNER_LOCK_NAME = "owner.lock"
# The claim's private name. The claim file is created, flocked, and written under
# it, then renamed onto `_OWNER_LOCK_NAME` (same inode, lock intact): no glob
# matches it, so a sweep can only ever see the final name already-locked (skip) or
# absent (not yet claimed) — never a claim it would judge stale mid-flight (#3629).
_CLAIM_LOCK_NAME = "owner.lock.claim"


def _throwaway_locks() -> list[Path]:
    """Every owner lock under any throwaway root
    (`shared.pg_throwaway_base.throwaway_roots`), sorted for a deterministic reap
    order. The single enumeration the sweep and the port registry share, so both
    see the same set of instance roots."""
    return sorted(
        lock
        for root in _throwaway_base.throwaway_roots()
        for lock in root.glob(f"{_THROWAWAY_PREFIX}*/{_OWNER_LOCK_NAME}")
    )


class _Registration(NamedTuple):
    """The held flock fd + its lock path. `fd` stays open for the instance's whole
    life: closing it is what tells the next sweep the owner is gone."""

    fd: int
    lock: Path


def _register_throwaway(instance_dir: Path, port: int) -> _Registration | None:
    """Claim `instance_dir` as this process's throwaway cluster by taking the
    lifetime flock on its own `owner.lock`. None on Windows (no flock, and the sweep
    is a no-op there too). The body carries owner diagnostics only — never a path.

    The claim is published already-locked — claim file, flock, body, rename — so
    `owner.lock` comes into existence only as the rename target of the file whose
    flock this process already holds. That is the invariant the sweep's whole
    judgment rests on (`flock acquirable => owner dead`): it can never open a
    created-but-unlocked `owner.lock` and reap a claim in flight (#3629)."""
    if IS_WINDOWS:
        return None
    import fcntl

    lock = instance_dir / _OWNER_LOCK_NAME
    claim = instance_dir / _CLAIM_LOCK_NAME
    fd = os.open(claim, os.O_RDWR | os.O_CREAT | os.O_TRUNC, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    os.write(fd, json.dumps({"owner_pid": os.getpid(), "port": port}).encode())
    claim.rename(lock)  # same inode: the published lock is the one already held
    return _Registration(fd, lock)


def _unregister_throwaway(registration: _Registration | None) -> None:
    """Release the flock and drop the lock file — the instance is torn down. The
    instance dir (and with it the lock) is usually already gone by this point; the
    close is what matters, and it cannot fail."""
    if registration is None:
        return
    os.close(registration.fd)
    registration.lock.unlink(missing_ok=True)


def _resolved_throwaway_dir(instance_dir: Path) -> Path | None:
    """`instance_dir` resolved, when it really is a throwaway instance dir this
    module created: `_THROWAWAY_PREFIX`-named, a real directory that resolves to a
    direct child of a throwaway root under that same name, holding a `data` entry
    that is not a symlink. None otherwise, and the sweeper then touches nothing.

    The name-preservation and non-symlink checks are what stop a stray symlink from
    aiming `pg_ctl` (or the rmtree) at a path outside every throwaway root."""
    if not instance_dir.name.startswith(_THROWAWAY_PREFIX) or not instance_dir.is_dir():
        return None
    resolved = instance_dir.resolve()
    roots = {root.resolve() for root in _throwaway_base.throwaway_roots()}
    if resolved.parent not in roots or resolved.name != instance_dir.name:
        return None
    if (resolved / "data").is_symlink():
        return None
    return resolved


class _OwnerRecord(TypedDict):
    """The owner.lock body this module writes: diagnostics only (`port` doubles as
    the allocation registry)."""

    owner_pid: int
    port: int


def _read_lock_body(fd: int) -> _OwnerRecord | None:
    """The JSON owner record from a lock fd, or None when unreadable. A run killed
    between creating the lock and writing it leaves a truncated body; that is a
    crash artifact, not a caller mistake, so it degrades to None rather than
    aborting the sweep or the port registry."""
    os.lseek(fd, 0, os.SEEK_SET)
    try:
        rec = json.loads(os.read(fd, 4096))
    except (ValueError, UnicodeDecodeError):
        return None
    if not isinstance(rec, dict):
        return None
    return cast(_OwnerRecord, rec)


def _owner_detail(fd: int) -> str:
    """Owner info from the lock body, for the reap log line. Diagnostics only — the
    reap target is the lock file's own parent."""
    rec = _read_lock_body(fd)
    if rec is None:
        return ""
    with contextlib.suppress(KeyError):
        return f" (owner pid {rec['owner_pid']}, port {rec['port']})"
    return ""


def _reap_locked(lock: Path, fd: int) -> int:
    """Reap the instance holding `lock` — its flock is held here, so its owner is
    provably gone. Returns 1 if an instance was removed."""
    if os.fstat(fd).st_nlink == 0:
        return 0  # a racing sweeper already reaped it between our open and our lock
    instance_dir = _resolved_throwaway_dir(lock.parent)
    if instance_dir is None:
        # Nothing safely reapable: the lock sits somewhere that is not one of this
        # module's instance dirs. Drop the stray lock, touch nothing else.
        lock.unlink(missing_ok=True)
        return 0
    detail = _owner_detail(fd)
    data = instance_dir / "data"
    if (data / "PG_VERSION").is_file():
        subprocess.run(  # noqa: S603 — argv is the resolved pg_ctl path + a verified data dir
            [pg_tool("pg_ctl"), "-D", str(data), "-m", "immediate", "stop"],
            check=False,
            capture_output=True,
        )
    shutil.rmtree(instance_dir, ignore_errors=True)
    logger.info(f"reaped orphaned throwaway postgres {instance_dir}{detail}")
    return 1


def sweep_orphaned_throwaway_clusters() -> int:
    """Stop and delete every throwaway cluster whose owning process is gone, and
    return how many were reaped. Called at the top of `throwaway_postgres`, so any
    run that starts one first bounds the leak from every previous killed run.

    Concurrency-safe (xdist workers all call it): the flock that proves deadness is
    the reap lock too, so one instance is reaped once, and a claim mid-flight is not
    observable — `owner.lock` is published only already-locked (see
    `_register_throwaway`). On a shared tmpfs another user's instance dirs are
    `0700`, so the glob simply does not see them. No-op on Windows."""
    if IS_WINDOWS:
        return 0
    import fcntl

    reaped = 0
    for lock in _throwaway_locks():
        try:
            fd = os.open(lock, os.O_RDWR)
        except OSError:
            continue  # vanished under us, or not ours to open — not ours to reap
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                continue  # a live process still owns this instance — leave it alone
            reaped += _reap_locked(lock, fd)
        finally:
            os.close(fd)
    return reaped


# ── Throwaway port allocation: serialized, so parallel workers never collide ──
#
# `_free_port` alone is a TOCTOU race: it binds port 0 (the OS picks a free port),
# closes the socket, and the caller's postmaster binds that port roughly a second
# later (after initdb). Two xdist workers in the same window can be handed the SAME
# port — the OS sees it free both times — and the second postmaster then fails with
# "Address already in use" (946 ThrowawayPgStartError on one 8-core CI run).
#
# The fix has two halves:
# - Allocation is atomic: under a host-wide flock on the throwaway root directory,
#   a worker probes the OS AND consults a registry of ports held by LIVE clusters
#   (the owner.lock bodies — the same flock liveness oracle as the sweep), then
#   registers its pick before releasing the lock. A port a live cluster is about to
#   bind is refused, so two live clusters can never be handed the same port even
#   though neither postmaster has bound it yet. The registry self-cleans exactly
#   like the sweep: a dead owner's flock is released by the kernel, and its port is
#   then safe — either the orphaned postmaster still holds it (the OS probe refuses
#   it) or it is truly free.
# - A start that loses the race anyway (a non-throwaway process bound the port in
#   the window, or Windows, which has no flock) is retried on a fresh port.
#
# The flock lives on the root DIRECTORY rather than a lock file: nothing to create
# (so no permission fights between users on a shared /dev/shm), nothing for the
# sweep globs to trip on, and the kernel drops it on process death like any flock.


def _live_throwaway_ports() -> set[int]:
    """Ports reserved by LIVE throwaway clusters — owner flock still held. Dead
    owners' entries are ignored: either the orphaned postmaster still holds the
    port (the OS probe refuses it anyway) or it is truly free. Only this module's
    instance dirs are consulted, so nothing outside the throwaway world can pin a
    port."""
    import fcntl

    ports: set[int] = set()
    for lock in _throwaway_locks():
        try:
            fd = os.open(lock, os.O_RDWR)
        except OSError:
            continue  # vanished under us, or not ours to open — not ours to read
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                rec = _read_lock_body(fd)
                if rec is not None and "port" in rec:
                    ports.add(int(rec["port"]))
            # flock acquired → owner dead; its port is reusable, and the sweeper
            # owns reaping the directory — just close and move on.
        finally:
            os.close(fd)
    return ports


def _port_lock_root() -> Path:
    """Where the host-wide port-allocation flock lives. Every allocator locks the
    SAME directory, whatever base its own instances use — a per-base lock would let
    two allocators serialize on different roots and hand out the same port. The
    platform default is settings-independent, so processes that disagree on
    AVA_PG_THROWAWAY_BASE still rendezvous here."""
    return _throwaway_base.default_base()


def _allocate_port(instance_dir: Path) -> tuple[int, _Registration | None]:
    """Reserve a localhost TCP port no LIVE throwaway cluster already holds, and
    register it under `instance_dir`'s owner lock. Probe, registry check, and
    registration all happen under one host-wide flock, so the reservation is
    visible to every other worker before ours is released. Windows has no flock:
    the plain probe is used there, and the retry in `throwaway_postgres` is the
    only protection."""
    if IS_WINDOWS:
        port = _free_port()
        return port, _register_throwaway(instance_dir, port)
    import fcntl

    root_fd = os.open(_port_lock_root(), os.O_RDONLY)
    try:
        fcntl.flock(root_fd, fcntl.LOCK_EX)
        try:
            while True:
                port = _free_port()
                if port not in _live_throwaway_ports():
                    break
            return port, _register_throwaway(instance_dir, port)
        finally:
            fcntl.flock(root_fd, fcntl.LOCK_UN)
    finally:
        os.close(root_fd)


# Stands in for pg.log content that was never written — postgres died before
# opening the log (initdb refusing, the postmaster never forking) or before
# writing its first line. Recorded rather than skipped: "the log is empty" is
# itself the diagnosis, while an absent artifact is indistinguishable from the
# capture being broken. Worded to stay true whether the file is missing or bare,
# since both land here.
_NO_PG_LOG_NOTE = "<no pg.log content — postgres never got far enough to write any>"


def _read_log_tail(log: Path, lines: int = 40) -> str:
    """The last `lines` of a pg.log, for the raised error's message. Best-effort:
    a log that vanished, is unreadable, or is empty degrades to "", which the
    caller renders as `_NO_PG_LOG_NOTE` — reading it must never mask the start
    failure it is describing."""
    with contextlib.suppress(OSError):
        if log.is_file():
            return "\n".join(log.read_text(errors="replace").splitlines()[-lines:])
    return ""


class ThrowawayPgStartError(subprocess.CalledProcessError):
    """A throwaway cluster failed initdb or `pg_ctl start`.

    Subclasses CalledProcessError so existing callers keep working, and carries
    the preserved pg.log path + tail so the failure reason survives the tmpfs
    teardown (issue #1037: 738 errors, one pg_ctl refusal, reason unrecoverable
    because the cluster lived in /dev/shm and was deleted on teardown)."""

    def __init__(
        self,
        exc: subprocess.CalledProcessError,
        log: Path,
        preserved: Path | None,
    ) -> None:
        super().__init__(exc.returncode, exc.cmd, output=exc.output, stderr=exc.stderr)
        self.preserved_log = preserved
        self.log_tail = _read_log_tail(log)

    def __str__(self) -> str:
        parts = [super().__str__()]
        if self.preserved_log is not None:
            parts.append(f"pg.log artifact: {self.preserved_log}")
        parts.append(f"--- pg.log tail ---\n{self.log_tail}" if self.log_tail else _NO_PG_LOG_NOTE)
        return "\n".join(parts)


def _preserve_failed_fixture_log(instance_dir: Path) -> Path | None:
    """Copy a failed throwaway cluster's pg.log out of the tmpfs before the
    teardown rmtree deletes it, writing `_NO_PG_LOG_NOTE` in its place when
    postgres never opened one. Best-effort: an unwritable artifact dir must not
    mask the start failure itself. Returns the artifact path, or None when
    nothing could be written."""
    log = instance_dir / "pg.log"
    dest_dir = _fixture_log_artifact_dir()
    try:
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / f"{instance_dir.name}.pg.log"
        if log.is_file():
            shutil.copy2(log, dest)
        else:
            dest.write_text(f"{_NO_PG_LOG_NOTE}\n")
        return dest
    except OSError:
        logger.warning(f"could not preserve failed fixture pg.log into {dest_dir}")
        return None


_PG_START_ATTEMPTS = 3


def _is_port_bind_failure(exc: subprocess.CalledProcessError, log: Path) -> bool:
    """True when a throwaway start failure is the port-collision race — another
    process bound our port between allocation and the postmaster's bind — rather
    than a genuine initdb/pg_ctl problem. The only failure worth retrying on a
    fresh port: retrying a real fault would burn attempts and mask it."""
    return "Address already in use" in f"{exc.stderr or ''}\n{_read_log_tail(log)}"


def _teardown_throwaway(
    tmp: Path,
    data: Path,
    registration: _Registration | None,
    process: subprocess.Popen[bytes] | None = None,
    *,
    foreground: bool = False,
) -> None:
    """Stop (if started), delete, and unregister a throwaway cluster. Shared by the
    retry loop (a lost start attempt is torn down before the next try) and the
    context exit."""
    if process is not None:
        stop_foreground_postgres(process)
    elif not foreground:
        subprocess.run(  # noqa: S603 — argv is the resolved pg_ctl path + static flags
            [pg_tool("pg_ctl"), "-D", str(data), "-m", "immediate", "stop"],
            check=False,
            capture_output=True,
        )
    shutil.rmtree(tmp, ignore_errors=True)
    _unregister_throwaway(registration)


@contextmanager
def throwaway_postgres(
    schema_sql: str | None = None, *, base: Path | None = None, foreground: bool = False
) -> Generator[str]:
    """initdb a throwaway Postgres cluster on an ephemeral port, optionally
    apply schema + checkpoint tables, and yield a psycopg connection URL.
    The cluster is destroyed on context exit.

    Entering first sweeps clusters leaked by killed runs (see the block above), so
    an interrupted run's orphan is bounded by the next spin-up rather than held
    until reboot.

    Port allocation is serialized across workers (see the block above), and a
    start that loses the race anyway is retried on a fresh port.

    Args:
        schema_sql: If given, applied after initdb and PostgresSaver.setup() is
            run too. If None, yields a bare `ava_citest` database (the caller applies
            its own DDL, e.g. the migration smoke replaying migrations/* onto a
            blank DB).
        base: where the instance dir is created. None — every existing caller —
            resolves through `shared.pg_throwaway_base.select_throwaway_base` (the
            configured override, else the platform default); a caller that knows its
            data footprint (the restore drill) passes a base from
            `select_throwaway_base(required_bytes)`, so the base it reports is the
            base used here.
        foreground: keep Postgres as a direct child in the caller's process group.
            Cancellable restore workers use this so their group reaper also owns
            the postmaster if the worker cannot finish normal cleanup.

    Yields:
        A postgresql:// URL string.
    """
    import psycopg

    sweep_orphaned_throwaway_clusters()
    tmp = data = Path()
    port = 0
    registration: _Registration | None = None
    process: subprocess.Popen[bytes] | None = None
    attempt = 0
    while True:
        attempt += 1
        tmp = Path(
            tempfile.mkdtemp(
                prefix=_THROWAWAY_PREFIX, dir=base or _throwaway_base.select_throwaway_base()
            )
        )
        data = tmp / "data"
        log = tmp / "pg.log"
        # Registered BEFORE initdb (inside `_allocate_port`): a kill in the window
        # between here and a running postmaster must still leave a record, or the
        # instance dir leaks unswept.
        port, registration = _allocate_port(tmp)
        try:
            try:
                subprocess.run(  # noqa: S603 — argv is the resolved initdb path + static flags
                    [
                        pg_tool("initdb"),
                        "-D",
                        str(data),
                        "-U",
                        "ava",
                        "-A",
                        "trust",
                        "--no-sync",
                        "--encoding=UTF8",
                        "--locale=C",
                    ],
                    check=True,
                    capture_output=True,
                )
                if foreground:
                    process = start_foreground_postgres(
                        [
                            str(pg_tool("postgres")),
                            "-D",
                            str(data),
                            "-p",
                            str(port),
                            "-c",
                            "listen_addresses=127.0.0.1",
                            "-c",
                            f"unix_socket_directories={tmp}",
                            "-c",
                            "fsync=off",
                            "-c",
                            "full_page_writes=off",
                            "-c",
                            "synchronous_commit=off",
                            *shlex.split(f"{pg_tz_args()} {pg_shm_args()}"),
                        ],
                        log=log,
                        env=pg_start_env(),
                    )
                    wait_foreground_postgres(process, log=log, port=port, data=data)
                else:
                    subprocess.run(  # noqa: S603 — argv is the resolved pg_ctl path + static flags
                        [
                            pg_tool("pg_ctl"),
                            "-D",
                            str(data),
                            "-l",
                            str(log),
                            "-w",
                            "-t",
                            "60",
                            "start",
                            # unix_socket_directories -> the writable tmp dir: Debian/Ubuntu
                            # defaults it to /var/run/postgresql (owned by the postgres user,
                            # not the non-root CI user), where the socket lock file can't be
                            # created. fsync/full_page_writes/synchronous_commit off: the data
                            # is disposable, so durability is pointless and skipping it keeps
                            # contended host disk I/O off the path.
                            "-o",
                            f"-p {port} -c listen_addresses=127.0.0.1 "
                            f"-c unix_socket_directories={tmp} "
                            "-c fsync=off -c full_page_writes=off -c synchronous_commit=off "
                            f"{pg_tz_args()} {pg_shm_args()}",
                        ],
                        check=True,
                        capture_output=True,
                        env=pg_start_env(),
                    )
            except subprocess.CalledProcessError as exc:
                if _is_port_bind_failure(exc, log) and attempt < _PG_START_ATTEMPTS:
                    # Lost the race: another process bound our port before the
                    # postmaster could. Tear this attempt down and try a fresh one.
                    _teardown_throwaway(tmp, data, registration, process, foreground=foreground)
                    process = None
                    continue
                # The cluster lives on a tmpfs and the teardown rmtree deletes it, so
                # without this the reason pg_ctl refused to start is unrecoverable after
                # the fact (issue #1037). Preserve pg.log into the artifact dir, then
                # re-raise with the tail attached.
                preserved = _preserve_failed_fixture_log(tmp)
                raise ThrowawayPgStartError(exc, log, preserved) from exc
            break
        except BaseException:
            _teardown_throwaway(tmp, data, registration, process, foreground=foreground)
            raise

    admin = f"postgresql://ava@127.0.0.1:{port}/postgres"
    url = f"postgresql://ava@127.0.0.1:{port}/ava_citest"

    try:
        with psycopg.connect(admin, autocommit=True) as conn, conn.cursor() as cur:
            # The test db is `ava_citest`, carried in the URL as data (names-as-data:
            # Settings re-applies only the password, never rewrites username/db, so a
            # subprocess loading Settings fresh dials exactly this URL). The peer
            # superuser role of the same name is kept for URLs that name it as the
            # user (trust auth ignores its password). Deliberately not `ava_main`
            # (the prod-db guard in tests/ava/conftest.py refuses that name).
            cur.execute("CREATE ROLE ava_citest LOGIN SUPERUSER")
            cur.execute("CREATE DATABASE ava_citest")

        if schema_sql is not None:
            from langgraph.checkpoint.postgres import PostgresSaver

            with psycopg.connect(url, autocommit=True) as conn, conn.cursor() as cur:
                cur.execute(schema_sql)  # type: ignore[arg-type]
            with PostgresSaver.from_conn_string(url) as saver:
                saver.setup()

        with stall_guard(admin, port):
            yield url
    finally:
        _teardown_throwaway(tmp, data, registration, process, foreground=foreground)
