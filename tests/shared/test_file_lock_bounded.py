"""`shared.platform.file_lock`'s bounded mode, and `.env`'s use of it.

The property under test is *cross-process*, so the tests spawn real interpreters.
An in-process test of a file lock proves almost nothing: `fcntl.flock` is advisory
per open-file-description and `msvcrt.locking` per handle, so two takes inside one
process can pass or fail for reasons that say nothing about two processes — which
is the case the lock exists for (a CLI converge, the gateway's config PUT and the
ops daemon all rewriting one `.env`).
"""

from __future__ import annotations

import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from shared.envfile import ENV_LOCK_TIMEOUT_S, env_lock_path
from shared.platform import LockTimeoutError, file_lock

_REPO = Path(__file__).resolve().parents[2]


def _spawn_holder(target: Path, ready: Path, hold_s: float) -> subprocess.Popen[bytes]:
    """A separate interpreter that takes the lock, signals, and holds it."""
    code = textwrap.dedent(f"""
        import pathlib, sys, time
        sys.path.insert(0, {str(_REPO)!r})
        from shared.platform import file_lock
        with file_lock(pathlib.Path({str(target)!r}), timeout_s=60):
            pathlib.Path({str(ready)!r}).write_text("1")
            time.sleep({hold_s})
    """)
    return subprocess.Popen(  # noqa: S603
        [sys.executable, "-c", code],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _await_ready(ready: Path, proc: subprocess.Popen[bytes]) -> None:
    deadline = time.monotonic() + 30
    while not ready.exists():
        assert proc.poll() is None, "the holder exited before taking the lock"
        assert time.monotonic() < deadline, "the holder never took the lock"
        time.sleep(0.02)


def test_a_second_process_waits_for_the_first(tmp_path: Path) -> None:
    """The guarantee itself: while another process holds it, this one does not get
    in — and once that process exits, it does."""
    target = tmp_path / ".env"
    ready = tmp_path / "ready"
    holder = _spawn_holder(target, ready, hold_s=3.0)
    try:
        _await_ready(ready, holder)

        started = time.monotonic()
        with file_lock(target, timeout_s=30):
            waited = time.monotonic() - started
        assert waited >= 0.4, f"took the lock while another process held it (waited {waited:.2f}s)"
    finally:
        holder.kill()
        holder.wait(timeout=10)


def test_the_wait_is_bounded_and_raises(tmp_path: Path) -> None:
    """Expiry raises rather than falling through to an unsynchronized write. A
    blocking wait with no bound is how one wedged holder becomes a wedged cluster;
    writing anyway would be the failure the lock exists to prevent, minus the
    error."""
    target = tmp_path / ".env"
    ready = tmp_path / "ready"
    holder = _spawn_holder(target, ready, hold_s=30.0)
    try:
        _await_ready(ready, holder)

        with pytest.raises(LockTimeoutError), file_lock(target, timeout_s=0.3):
            pytest.fail("took a lock another process was holding")
    finally:
        holder.kill()
        holder.wait(timeout=10)


def test_the_lock_is_released_when_a_holder_dies(tmp_path: Path) -> None:
    """No stale-lock handling, because the OS drops the lock when the process goes
    — including on a crash. A lock FILE left behind is not a held lock, and code
    that tried to clean it up would be deciding on evidence it does not have."""
    target = tmp_path / ".env"
    ready = tmp_path / "ready"
    holder = _spawn_holder(target, ready, hold_s=30.0)
    _await_ready(ready, holder)
    holder.kill()
    holder.wait(timeout=10)

    with file_lock(target, timeout_s=10):
        pass  # the dead holder's lock did not outlive it


def test_env_locking_never_touches_the_env_file_itself(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every door must lock a SIBLING. `file_lock`'s POSIX branch opens its path with
    "w", which truncates — pointed at the real `.env` it would empty a cluster's
    secrets outright, which is the accident this guard exists for."""
    import shared.runtime_config as rc
    from shared.envfile import upsert_env

    env = tmp_path / ".env"
    env.write_text("AVA_KEEP=1\n")
    monkeypatch.setattr(rc, "env_file_path", lambda: env)

    assert env_lock_path(env) != env
    rc.write_fields({}, set())
    upsert_env(env, {"AVA_NEW": "2"})

    body = env.read_text()
    assert "AVA_KEEP=1" in body, "the lock truncated the file it guards"
    assert "AVA_NEW=2" in body


def test_the_env_wait_is_bounded_in_seconds() -> None:
    """The bound is the whole difference from the unbounded default the registry
    still uses; an edit that made it hours would restore the failure mode without
    touching any call site."""
    assert 1.0 <= ENV_LOCK_TIMEOUT_S <= 120.0


def test_the_unbounded_default_is_unchanged(tmp_path: Path) -> None:
    """No timeout means the historical blocking behaviour, which the cluster
    registry and `crontab_lock` still rely on — the bound is opt-in, so adding it
    could not have changed them."""
    lock = tmp_path / "reg.lock"
    with file_lock(lock):
        pass
    assert lock.exists()


def _recorded_lock(monkeypatch: pytest.MonkeyPatch, module: object, taken: list[Path]):  # type: ignore[no-untyped-def]
    """Wrap `module.file_lock` so a test can see which path it took."""
    real = module.file_lock  # type: ignore[attr-defined]

    def _record(target: Path, *, timeout_s: float | None = None):  # type: ignore[no-untyped-def]
        taken.append(target)
        return real(target, timeout_s=timeout_s)

    monkeypatch.setattr(module, "file_lock", _record)


def test_every_env_write_door_takes_the_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """All FOUR doors, not just the one this started with.

    The first version of this change locked `write_fields` alone and claimed the
    CLI-converge / gateway-PUT / ops-daemon race was closed. It was not: converge
    writes `.env` through `upsert_env` on **every `ava start`**, `remove_env` and
    `rename_env_keys` are two more rewrites, and `enroll`'s `write_bootstrap_env`
    replaces the file wholesale. A lock on one door orders nothing.
    """
    import cli.enroll as enroll_mod
    import shared.envfile as envfile_mod
    import shared.runtime_config as rc

    env = tmp_path / ".env"
    env.write_text("AVA_OLD=1\n")
    monkeypatch.setattr(rc, "env_file_path", lambda: env)
    expected = env_lock_path(env)

    taken: list[Path] = []
    _recorded_lock(monkeypatch, rc, taken)
    _recorded_lock(monkeypatch, envfile_mod, taken)
    _recorded_lock(monkeypatch, enroll_mod, taken)

    rc.write_fields({}, set())
    envfile_mod.upsert_env(env, {"AVA_NEW": "2"})
    envfile_mod.remove_env(env, {"AVA_OLD"})
    rc.rename_env_keys(env, {"AVA_NEW": "AVA_RENAMED"})
    enroll_mod.write_bootstrap_env(env, gateway="http://g", machine_name="m")

    assert len(taken) == 5, f"a door wrote .env without the lock: {taken}"
    assert set(taken) == {expected}


def test_the_write_doors_are_leaves(tmp_path: Path) -> None:
    """None of the doors may call another. `fcntl` locks are per open file
    description, so a nested take blocks on itself and only ever ends as a
    `LockTimeoutError` after the full bound — a self-deadlock wearing a timeout.
    Asserted on the sources so the invariant survives a future edit."""
    import inspect

    import cli.enroll as enroll_mod
    import shared.envfile as envfile_mod
    import shared.runtime_config as rc

    doors = {
        "upsert_env": envfile_mod.upsert_env,
        "remove_env": envfile_mod.remove_env,
        "rename_env_keys": rc.rename_env_keys,
        "write_bootstrap_env": enroll_mod.write_bootstrap_env,
        "write_fields": rc.write_fields,
    }
    for name, fn in doors.items():
        body = inspect.getsource(fn)
        for other in doors:
            if other == name:
                continue
            assert f"{other}(" not in body, f"{name} calls {other} — nested lock take"
